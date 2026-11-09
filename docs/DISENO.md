# Diseño: traductor de sesiones Qoder ⇄ CodeBuddy ⇄ OpenCode

> Documento de diseño original de la sesión de CodeBuddy (2026-11-08),
> conservado como referencia histórica. Algunas rutas y detalles pueden
> haber cambiado respecto al código actual.


## Contexto

El usuario usa Qoder, CodeBuddy y OpenCode (los tres son CLIs de coding agent). Cada uno
tiene límites de uso distintos y, cuando se agota el de uno, quiere **continuar la misma
conversación en otro**. Hoy las sesiones viven en formatos incompatibles:

- **Qoder**: `~/.qoder/projects/<slug>/<id>.jsonl` — estilo Claude Code (`type: user/assistant`, `message.content` con bloques `text`/`thinking`/`tool_use`/`tool_result`, `uuid`/`parentUuid`).
- **CodeBuddy**: `~/.codebuddy/projects/<slug>/<id>.jsonl` — formato propio (`message` rol user, `reasoning`, `function_call`, `function_call_result`, `ai-title`, …).
- **OpenCode**: SQLite `~/.local/share/opencode/opencode.db`. Por suerte expone `opencode session export/import` (JSON `{info, messages}`), así que **no tocamos la BD directamente**: importamos un JSON con `opencode session import --directory <cwd>`.

Objetivo: una herramienta `sessconv` que convierte una sesión entre cualquiera de los tres
formatos, preservando el contexto (mensajes usuario/asistente, razonamiento, tool calls/results,
cwd y título) para poder reanudarla con otro tool.

**Restricción del usuario**: "solo test de funcionamiento, no pruebes con inferencia". La
verificación usará solo comandos de listado/carga de cada CLI (sin llamar al modelo), nunca
generará tokens de reasoning.

## Formato intermedio (canónico)

Modelo neutral en memoria, una sola representación de la que cuelgan los 3 adapters:

```
Session = {
  id, title, cwd, model (str|None), created_at (epoch_ms), updated_at (epoch_ms),
  messages: [ Message ]
}
Message = {
  role: "user" | "assistant",
  timestamp (epoch_ms),
  parts: [ Part ]
}
Part = {type:"text", text}
     | {type:"reasoning", text}
     | {type:"tool_call", id, name, args(dict), status}
     | {type:"tool_result", call_id, name, output(str), is_error}
```

Se ignoran líneas meta (qoder `workspace-directories`, `runtime-config`, `active-leaf`,
`attachment`, `file-history-snapshot`, `last-prompt`; codebuddy `file-history-snapshot`,
`turn-metrics`, `session-meta`; opencode `idle`, `synthetic`, `system`, `compaction`).

## Herramienta: `~/.local/bin/sessconv`

Python 3 solo stdlib (sin dependencias), ejecutable (está en PATH). Un solo archivo.

```
sessconv list   [--from {qoder|codebuddy|opencode}] [--cwd DIR] [--json]
sessconv inspect <id-o-path> --from TOOL [--json]      # vuelca el modelo canónico
sessconv convert <id-o-path> --from TOOL --to TOOL
        [--cwd DIR] [--title TITULO] [--dry-run] [--print-id]
sessconv roundtrip <id> --from TOOL --via TOOL2          # fidelidad en memoria, sin escribir
sessconv selftest                                          # fixtures en memoria (ci/demo)
```

- `<id-o-path>` acepta el id de sesión o la ruta directa a un `.jsonl`/`.json` exportado.
- `--from` se infiere por patrón de id si se omite: `ses_…`→opencode, UUIDv7 (`01a1…`)→codebuddy, UUIDv4→qoder; o por extensión/ruta.
- `--cwd` por defecto = directorio actual; define el `<slug>` de destino.

### Slug de proyecto
- Qoder: `cwd.replace("/", "-")` (mantiene guion inicial, p.ej. `/home/leonisauro`→`-home-leonisauro`).
- CodeBuddy: igual pero recortando el guion inicial (`-home-leonisauro`→`home-leonisauro`).
- OpenCode: se pasa `--directory <cwd>` al import; el proyecto se resuelve/crea solo.

### Adapters

**read/write Qoder** (`~/.qoder/projects/<slug>/<id>.jsonl`)
- read: líneas `user`/`assistant` → Message; `message.content` bloques: `text`→text, `thinking`→reasoning, `tool_use`→tool_call, `tool_result`→tool_result. `isMeta`/`<local-command-caveat>`→omitir.
- write: nuevo **UUIDv4**; emite `workspace-directories`, `runtime-config` (model `qfmodel`), `worktree-state`, luego las líneas `user`/`assistant` con cadena `uuid`/`parentUuid`, `cwd`, `sessionId`, `version` `1.1.85`, `gitBranch` (si aplica), `ai-title`, y `active-leaf` final. Tool result → línea `user` con `content:[{type:"tool_result",tool_use_id,content}]` (+ `toolUseResult` mínimo).

**read/write CodeBuddy** (`~/.codebuddy/projects/<slug>/<id>.jsonl`)
- read: `message` rol user→text; `reasoning`→reasoning (de `rawContent[].text`); `function_call` (`name`, `arguments` string JSON, `callId`)→tool_call; `function_call_result` (`name`, `callId`, `status`, `output.text`)→tool_result; `message` rol assistant `output_text`→text.
- write: nuevo **UUIDv7** (48 bits ms + random); emite `message`(user) con `content:[{type:"input_text",text}]`, `reasoning` con `rawContent:[{type:"reasoning_text",text}]`, `function_call` (campos `name`,`arguments`=JSON string,`callId`), `function_call_result` (`name`,`callId`,`status`,`output:{type:"text",text}`), assistant `message` `output_text`, `ai-title`, `summary`. Todo con `sessionId`,`cwd`,`timestamp`,`id`, cadena `parentId`.

**read/write OpenCode** (vía CLI, sin tocar la BD)
- read: `opencode session export <id>` → `info`/`messages`; `user`→text, `assistant`→text+reasoning+tool parts (`content[].type` `reasoning`/`text`/`tool`; tool trae `id`,`name`,`state.input`,`state.content[].text`, error en `state.status=="error"`).
- write: construye el JSON export `{info:{agent:"build",model:{id:model or "qfmodel",providerID:"opencode-go"},title,cost:0,tokens:{...},time:{created,updated},location:{directory:cwd}}, messages:[...]}` y ejecuta `opencode session import <tmpfile> --directory <cwd>`. El `info.id` se deja que OpenCode asigne (o se propone `ses_`+base62) — se confirmará en el test de import.

### Mapeo de herramientas (nombre + argumentos)
Diferencias clave observadas:

| semántica | Qoder/CodeBuddy | OpenCode |
|---|---|---|
| shell | `Bash` (`command`,`description`) | `shell` (`command`) |
| leer | `Read` (`file_path`) | `read` (`path`) |
| escribir | `Write` (`file_path`,`content`) | `write` (`path`,`content`) |
| editar | `Edit` (`file_path`,`old_string`,`new_string`) | `edit` (`path`,`oldString`,`newString`) |
| glob/grep | `Glob`/`Grep` (`pattern`,`path`) | `glob`/`grep` (igual) |
| web | `WebFetch` (`url`,`prompt`) | `webfetch` (`url`,`prompt`) |
| imagen | `ViewImage` (`image_path`,`query`) | (lee vía `read`) |

Tabla bidireccional en el adapter. `args` se re-empaquetan con las claves correctas por tool.
Tools sin mapeo → se conservan igual (fallback).

### Imágenes (limitación conocida)
Los blobs de imagen (ViewImage/CodeBuddy `blobs/`, qoder image blocks) se conservan como
**texto placeholder** con la ruta origen si está disponible, si no `[imagen omitida]`. No se
reenvían bytes binarios entre herramientas. Esto es aceptable para "retomar la conversación"
(texto + tool calls). Se documenta en el `--help`.

## Seguridad (importante)
- Siempre se genera un **id nuevo** de destino: nunca se sobreescribe ninguna sesión existente.
- Si el archivo/import de destino ya existe, `convert` aborta (no overwrite).
- OpenCode: `session import` crea sesión nueva (seguro); nunca `DELETE`/UPDATE sobre la BD.
- Las pruebas crean sesiones marcadas con título `SESSCONV-TEST …` y se **eliminan al final**
  (solo las que creamos, por id/título), previa confirmación del usuario. No se tocan sesiones reales.
- `--dry-run` escribe a stdout en vez de disco para revisar antes de materializar.

## Verificación (sin inferencia)
1. **Unitarias en memoria**: `sessconv roundtrip` y `selftest` — leen una sesión real de cada
   tool, la reconvierten a canónico y la vuelven a emitir en el formato intermedio, comparando
   conteo de mensajes/parts y textos. Sin escribir nada.
2. **Aceptación por el CLI (sin tokens)**:
   - Generar 1 sesión de prueba nueva por cada conversión real (p.ej. qoder→opencode, codebuddy→qoder, opencode→codebuddy).
   - **Qoder**: `cd <cwd> && qoder --list-sessions` → la sesión `SESSCONV-TEST` debe aparecer (ya comprobado que este comando lista desde el `.jsonl` sin inferencia).
   - **OpenCode**: `opencode session list --format json` en `<cwd>` → la nueva `ses_…` aparece; y `opencode session export <nuevo-id>` round-trip válido.
   - **CodeBuddy**: `timeout 8 script -qec "codebuddy -r" /dev/null` captura el picker interactivo y se busca el título `SESSCONV-TEST`; luego se manda ESC. (Sin prompt, sin API.)
3. **Fidelidad**: comparar número de mensajes user/assistant y de tool calls entre origen y destino con `sessconv inspect`.
4. Limpiar las sesiones de prueba (eliminar archivos jsonl creados y, para opencode, `opencode session delete <id>`) — solo las marcadas `SESSCONV-TEST`.

## Archivos a crear
- `/home/leonisauro/.local/bin/sessconv` (ejecutable, Python3, ~1 archivo, stdlib).

## Riesgos / a confirmar durante la implementación
- Forma exacta que acepta `opencode session import` (campos obligatorios de `info`): se probará
  exportando una sesión real, re-importándola y comparando con `export`.
- Si `codebuddy -r` no descubre sesiones solo por el `.jsonl`, habrá que localizar el índice que
  usa (posible `history.jsonl` o escaneo de dir) e implementar la escritura mínima equivalente;
  el test del paso 2b lo detecta antes de dar por bueno.
