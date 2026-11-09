# sessconv

Traductor de sesiones entre **Qoder**, **CodeBuddy** y **OpenCode**.

Convierte una conversación de un CLI de coding agent a otro preservando el
contexto —mensajes de usuario/asistente, razonamiento, tool calls, resultados,
`cwd` y título— para poder retomarla con otra herramienta cuando se agota el uso
de una de ellas.

- Un solo archivo Python 3, solo biblioteca estándar.
- No llama al modelo ni gasta tokens para convertir.
- Nunca sobreescribe una sesión existente: siempre genera IDs nuevos.

> Recuperado de una sesión de CodeBuddy del 2026-11-08. El estado del proyecto
> es funcional pero experimental; los formatos internos de los tres CLIs pueden
> cambiar sin aviso.

## Requisitos

- Python 3.8 o superior.
- Las herramientas implicadas, según los formatos que vayas a usar:
  - [OpenCode](https://opencode.ai) (`opencode` en el `PATH`, necesario para
    exportar/importar sesiones).
  - Qoder y CodeBuddy solo necesitan sus directorios de datos.

## Instalación

```bash
git clone https://github.com/Leonisaurov/sessconv.git
cd sessconv
install -m 755 sessconv.py ~/.local/bin/sessconv
```

O sin copiar:

```bash
ln -sf "$PWD/sessconv.py" ~/.local/bin/sessconv
```

## Uso

```text
sessconv list    [--from {qoder|codebuddy|opencode}] [--cwd DIR] [--json]
sessconv inspect <id-o-ruta> --from TOOL [--json]
sessconv convert <id-o-ruta> --from TOOL --to TOOL [--cwd DIR] [--title T] [--dry-run] [--print-id]
sessconv roundtrip <id> --from TOOL --via TOOL2
sessconv selftest
```

### Ejemplos

Listar las sesiones de Qoder:

```bash
sessconv list --from qoder
```

Ver el modelo canónico de una sesión:

```bash
sessconv inspect 0dd985da-5b3c-487e-a03a-5cb8d2efc057 --from qoder
```

Convertir una sesión de CodeBuddy a Qoder:

```bash
sessconv convert 01a1be1a-5169-7879-a682-b293adc0be5f \
  --from codebuddy --to qoder
```

Revisar el resultado sin escribir nada:

```bash
sessconv convert <id> --from codebuddy --to opencode --dry-run
```

Comprobar fidelidad en memoria (sin escribir):

```bash
sessconv roundtrip <id> --from codebuddy --via qoder
```

Prueba interna con un fixture:

```bash
sessconv selftest
```

`<id-o-ruta>` acepta el ID de la sesión o la ruta directa a un `.jsonl` o a un
JSON exportado. Si se omite `--from`, se infiere por el patrón del ID
(`ses_…` → OpenCode, UUIDv7 → CodeBuddy, UUIDv4 → Qoder) o por la ruta.

## Cómo funciona

### Modelo canónico

Los tres adaptadores leen y escriben un modelo neutral en memoria:

```text
Session = { id, title, cwd, model, created_at, updated_at, messages[] }

Message = { role: "user" | "assistant", timestamp, parts[] }

Part = { type: "text", text }
     | { type: "reasoning", text }
     | { type: "tool_call", id, name, args, status }
     | { type: "tool_result", call_id, name, output, is_error }
```

### Formatos

| Herramienta | Ubicación | Mecanismo |
|---|---|---|
| Qoder | `~/.qoder/projects/<slug>/<id>.jsonl` | JSONL estilo Claude Code |
| CodeBuddy | `~/.codebuddy/projects/<slug>/<id>.jsonl` | JSONL propio |
| OpenCode | `~/.local/share/opencode/opencode.db` | CLI `session export/import` (no se toca SQLite) |

Al importar en OpenCode se genera un `info.id` nuevo (`ses_…`), IDs de mensaje
`msg_…` y un `projectID` no vacío; OpenCode resuelve el proyecto real a partir
de `--directory`. Los `tool_result` de Qoder/CodeBuddy se incrustan en el
`tool_call` correspondiente, y los fallos usan el objeto `state.error` que
valida OpenCode.

### Mapeo de herramientas

| Semántica | Qoder / CodeBuddy | OpenCode |
|---|---|---|
| shell | `Bash` (`command`, `description`) | `shell` (`command`) |
| leer | `Read` (`file_path`) | `read` (`path`) |
| escribir | `Write` (`file_path`, `content`) | `write` (`path`, `content`) |
| editar | `Edit` (`file_path`, `old_string`, `new_string`) | `edit` (`path`, `oldString`, `newString`) |
| glob/grep | `Glob` / `Grep` | `glob` / `grep` |
| web | `WebFetch` (`url`, `prompt`) | `webfetch` (`url`, `prompt`) |
| imagen | `ViewImage` (`image_path`, `query`) | `read` |

Las herramientas sin mapeo se conservan tal cual.

## Seguridad

- Cada conversión crea una sesión nueva con un ID nuevo.
- Si el archivo de destino ya existe, `convert` aborta sin sobreescribir.
- OpenCode se importa mediante `opencode session import`; nunca se ejecutan
  `UPDATE` ni `DELETE` sobre su base de datos.
- `--dry-run` escribe el resultado en stdout para revisarlo antes.

## Estado verificado

Probado sin llamar al modelo (2026-11-08, OpenCode v2.2.6):

| Conversión | Resultado |
|---|---|
| Qoder → CodeBuddy (roundtrip) | 626/626 `parts` |
| CodeBuddy → Qoder (roundtrip) | 333/333 `parts` |
| Qoder → OpenCode (import real) → Qoder | 467 mensajes, 626/626 `parts` |
| OpenCode → CodeBuddy (archivo) → OpenCode (import real) | 626/626 `parts` |
| Qoder: sesión convertida visible en `qoder --list-sessions` | OK |
| CodeBuddy: sesión convertida visible en el selector `codebuddy -r` | OK |
| `selftest` | OK en los tres formatos |

Las sesiones de prueba se marcaron con `SESSCONV-TEST` y se eliminaron al
terminar.

## Limitaciones

- Las imágenes se representan como texto placeholder (`[imagen omitida]` o la
  ruta de origen); no se transfieren bytes entre herramientas.
- La fidelidad se mide por tipos de `parts` (texto, razonamiento, tool calls y
  resultados), no por número de mensajes: Qoder y CodeBuddy separan en varias
  líneas lo que OpenCode guarda en un solo mensaje.
- `roundtrip` comprueba Qoder y CodeBuddy en memoria. Para OpenCode hay que usar
  `convert` (import real) y verificar con `inspect`; así se evita escribir en la
  base de datos durante una comprobación rápida.
- Los formatos no son APIs públicas y pueden cambiar. Si una versión nueva de
  cualquiera de los tres CLIs rompe la conversión, hay que ajustar su adaptador.

## Estructura

```text
.
├── sessconv.py     # herramienta completa (stdlib)
├── docs/
│   └── DISENO.md   # notas de diseño originales de la sesión
└── README.md
```

## Licencia

Sin licencia definida. Todos los derechos reservados por el autor.
