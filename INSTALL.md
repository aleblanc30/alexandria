# Installing Alexandria on Windows

This describes a standalone installation, separate from any development
checkout, that starts automatically at logon and serves the UI at
`http://localhost:8420`.

The install is pinned to a release tag. Substitute the tag you are installing
for `v0.0.5` throughout.

## 1. Prerequisites

Install these first, from their own installers:

- Python 3.11 or later, with `python` on `PATH`
- Node.js (LTS), with `npm` on `PATH`
- Git
- [Ollama](https://ollama.com), which on Windows runs as a tray app started at
  logon

Then raise the path-length limit, since a nested path under AppData plus
`node_modules` will otherwise exceed the legacy 260-character maximum:

```bat
git config --global core.longpaths true
```

Verify the three toolchains before continuing:

```bat
python --version
node --version
git --version
```

## 2. Directory layout

Code and library data are kept in sibling directories so that reinstalling or
upgrading the application cannot disturb an ingested library:

```
%LOCALAPPDATA%\Alexandria\
    app\      the clone, pinned to a release tag
    data\     SQLite database, Chroma directory, model caches
```

Use `%LOCALAPPDATA%` rather than `%APPDATA%`. The latter is the roaming
profile, intended for small configuration files that follow a user between
machines. This installation carries a virtual environment containing torch, a
`node_modules` tree, EasyOCR recognition models, and eventually the vector
store — several gigabytes, which on a domain-joined machine would attempt to
synchronise at every logon.

## 3. Clone and build

```bat
mkdir "%LOCALAPPDATA%\Alexandria\data"
git clone https://github.com/aleblanc30/alexandria "%LOCALAPPDATA%\Alexandria\app"
cd /d "%LOCALAPPDATA%\Alexandria\app"
git checkout v0.0.5
```

Create the virtual environment and install the package:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install .
```

`pip install .` rather than `pip install -e .`. A non-editable install means
this tree is inert if it is ever opened in an editor, so the running
installation cannot be changed by accident.

Build the frontend:

```bat
cd frontend
npm install
npm run build
cd ..
```

Run these as separate commands rather than chaining them with `&&` on one
line. A failed `npm install` is easy to miss when its output scrolls past, and
the resulting symptom (see §9) is unhelpfully indirect.

Confirm the build produced output before continuing:

```bat
dir frontend\dist\index.html
```

## 4. Configuration

Copy both templates in `app\`:

```bat
copy .env.example .env
copy .secrets.example .secrets
```

In `.env`, set `ALEXANDRIA_DATA_DIR` to an absolute path:

```
ALEXANDRIA_DATA_DIR=C:\Users\<you>\AppData\Local\Alexandria\data
```

That is the only storage setting there is. The SQLite file (`archive.db`), the
Chroma directory (`chroma`), the cached OAuth token and the Zotero/Firefox
source snapshots are all derived from it, so nothing else needs pointing at the
data directory. Its default is the relative path `data`, which resolves against
the process working directory and would therefore move the library depending on
where the server was started from; an absolute path removes that dependency.

Credentials belong in `.secrets`, not `.env`: the Reddit feed URL, and any
API keys for optional providers. Both files are git-ignored.

Note that `Settings` rejects unknown `ALEXANDRIA_*` keys, so a stale entry
copied from an older configuration will prevent startup rather than being
ignored.

Initialise the database:

```bat
alexandria init
```

This creates `archive.db` under `data\`, and nothing outside it. The `chroma`
subdirectory is not created yet — it appears on the first write to the vector
store — so a `data\` holding only the database at this point is expected.
Checking now confirms the path configuration took effect, before any expensive
ingestion depends on it.

## 5. Server launcher

The repository ships this launcher at `scripts\start-server.bat`, so there is
nothing to write — use it where it is. A second copy at the app root would drift
the moment an upgrade updates the tracked one and leaves the copy behind.

```bat
@echo off
cd /d "%LOCALAPPDATA%\Alexandria\app"
call .venv\Scripts\activate.bat
echo [%date% %time%] starting cwd=%CD% >> "%LOCALAPPDATA%\Alexandria\data\server.log"
uvicorn pka.api.main:app --host 127.0.0.1 --port 8420 >> "%LOCALAPPDATA%\Alexandria\data\server.log" 2>&1
```

It writes the log beside the library, in the `data\` sibling from §2, which is
the layout this document assumes throughout; edit those two paths if you set
`ALEXANDRIA_DATA_DIR` somewhere else.

The redirection matters once the server runs hidden, since it becomes the only
record of a failed start. The `--host 127.0.0.1` binding is uvicorn's default,
but is stated explicitly because the API has no authentication.

Run this by hand once and open `http://localhost:8420`. Note how long a cold
start takes and how much resident memory the process settles at: `torch`,
`transformers` and `easyocr` are dependencies, and whether they are imported
eagerly determines whether an always-running server is a negligible or a
substantial background cost. If it proves heavy, launching on demand from a
shortcut may suit better than the autostart described below; §8 covers
both arrangements.

## 6. Hidden-window wrapper

The Task Scheduler "Hidden" checkbox hides the task from the scheduler's own
library listing. It does not suppress windows opened by the program the task
runs, and a task configured to run only when the user is logged on executes in
the interactive session, where `cmd.exe` acquires a console window. The Windows
Script Host has no console of its own and can set the window style of a process
it spawns.

Create `%LOCALAPPDATA%\Alexandria\app\start-hidden.vbs`:

```vbs
Option Explicit
Dim shell, base, rc
Set shell = CreateObject("WScript.Shell")
base = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Alexandria\app")
shell.CurrentDirectory = base
rc = shell.Run("""" & base & "\scripts\start-server.bat""", 0, True)
WScript.Quit rc
```

The `0` is the window style, meaning hidden. The `True` is `bWaitOnReturn`,
and it is deliberate: with it, `wscript.exe` remains alive for as long as
uvicorn runs and exits with uvicorn's exit code. Task Scheduler therefore
reports the task as running while the server is up, ending the task terminates
the server, and a restart-on-failure policy fires when the server dies. With
`False`, the task would complete successfully within a second and none of those
three behaviours would work.

If Windows Script Host is disabled by policy or by endpoint-protection
software on the machine, the equivalent is invoking the batch file through
`powershell.exe -WindowStyle Hidden -Command`, which briefly flashes a window
where `wscript` does not.

## 7. Scheduled task

In Task Scheduler, choose Create Task rather than Create Basic Task, which does
not expose all the settings below.

**General**: run only when user is logged on.

**Triggers**: at log on, for your user account, delayed 30 seconds.

**Actions**: start a program.

| Field | Value |
| --- | --- |
| Program/script | `C:\Windows\System32\wscript.exe` |
| Add arguments | `"C:\Users\<you>\AppData\Local\Alexandria\app\start-hidden.vbs"` |
| Start in | `C:\Users\<you>\AppData\Local\Alexandria\app` |

Write these two paths out in full rather than using `%LOCALAPPDATA%`. Task
Scheduler's expansion of environment variables in the arguments field is
inconsistent, and the failure mode is a task that silently does nothing.

**Settings**: enable restart on failure, every 1 minute, up to 3 times. Clear
"Stop the task if it runs longer than 3 days", which is enabled by default and
would otherwise terminate the server after 72 hours of uptime.

Name the task `Alexandria`, so that the stop shortcut below can reference it.

Running the task manually should produce no visible window, a Status of
"Running" that persists, a `python.exe` or `uvicorn.exe` entry under Task
Manager's Details tab, and a working `http://localhost:8420`. A status that
returns to "Ready" immediately means the server exited; consult
`data\server.log`.

## 8. Shortcuts

What these do depends on whether you set up §7. With the scheduled task in
place the server is already running at logon, so the shortcuts only open and
close an interface that already exists. Without it — the on-demand
arrangement raised in §5, if a permanently resident server proves too heavy —
"Start Alexandria Server" is what brings the server up, and the console is the
only place its output is visible.

Place these in `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Alexandria\`,
and copy the first to the Desktop. This location is per-user and needs no
administrator rights; it is roaming AppData, which is appropriate for shortcut
files. Create each with right-click → New → Shortcut in that folder, pasting
the whole target line below, quotes included, into the wizard's location field.

| Shortcut | Target |
| --- | --- |
| Alexandria | `http://localhost:8420` (internet shortcut) |
| Start Alexandria Server | `C:\Windows\System32\wscript.exe "C:\Users\<you>\AppData\Local\Alexandria\app\start-hidden.vbs"` |
| Open Alexandria Console | `C:\Users\<you>\AppData\Local\Alexandria\app\scripts\console.bat` |
| Stop Alexandria | `C:\Users\<you>\AppData\Local\Alexandria\app\scripts\stop-server.bat` |
| Alexandria files | `%LOCALAPPDATA%\Alexandria\app` |

The last is worth having because AppData is hidden in Explorer by default and
`.env` and `.secrets` live there.

**Start Alexandria Server** runs the §6 wrapper, so write that file even if you
skipped the scheduled task: it is what suppresses the console window the batch
launcher would otherwise open. Nothing visible happens when the shortcut is
used. Allow the cold start you timed in §5 before opening the Alexandria
shortcut. Using it while the server is already up starts a second uvicorn that
binds a taken port and exits within a second; the first server is unaffected,
and the failure is recorded in the log rather than shown (§9).

**Open Alexandria Console** runs `scripts\console.bat`, which follows
`data\server.log` — the last 200 lines, then each new one as it is written.
Once the server runs hidden this is the only view of its output, and so where a
failed start, an ingestion traceback or a request error appears. It only reads:
closing the window leaves the server running, and opening it before the server
has ever run waits for the file to appear rather than failing.

**Stop Alexandria** runs `scripts\stop-server.bat`, which ends the `Alexandria`
task if one is registered and then stops whatever still holds port 8420. It
covers both arrangements for that reason — ending the task is enough under §7,
while an on-demand server has no task to end. Note that it goes by the port
rather than by identity, so if a development checkout is serving 8420
(`alexandria dev`, per §9), that is what it will stop.

Both scripts assume the default port and the `data\` location from §2, as the
§5 launcher does. The `LOG` line at the top of `console.bat` and the `PORT`
line at the top of `stop-server.bat` are what to edit if you moved either.

## 9. Troubleshooting

**A JSON body reading `{"detail":"Not Found"}` at the root URL.** The backend
is running and answering; what is missing is the static file mount. The
application mounts `frontend/dist` at `/` inside a `try` block that discards
the error when the directory is absent, so the only symptom is a 404.

Confirm the backend is otherwise healthy by opening `http://localhost:8420/docs`,
which should render the API documentation. Then establish which of two causes
applies:

- If `app\frontend\dist\index.html` does not exist, the build did not run or
  did not succeed. Repeat §3, reading the `npm` output.
- If it does exist, the working directory is wrong at startup, because the
  mount path is resolved relative to the process working directory rather than
  to the package location. The `cwd=` line written to `server.log` by the
  launcher in §5 reports what it actually was. A typo in either the batch
  file's `cd /d` or the script's `CurrentDirectory` lands the process in
  `C:\Windows\System32`, where the directory is absent and the error is
  swallowed.

In both cases restart the task afterwards. The mount is evaluated once, when
the module is imported, so building the frontend under a running server changes
nothing until it restarts.

**Ingestion or clustering fails immediately.** Confirm the Ollama tray app is
running and that the models the configuration resolves to have been pulled. On
the defaults those are `moondream` for the image admission gate and `llava` for
vision. Chat has no default model *name*: `ALEXANDRIA_CHAT_MODEL` is empty out
of the box, and the provider lists the daemon's models and takes the first that
is not an embedding model, falling back to `llama3` if that listing fails or
comes back empty. Any one chat model being present is therefore enough to run,
but which one gets used is whatever Ollama returns first — set the key
explicitly if that matters.

**Port 8420 already in use.** A development checkout running `alexandria dev`
uses the same port. Assign one of the two a different port.

## 10. First ingestion

Run one source with `--dry-run` first, then a small real run, before ingesting
the whole library. Ingestion is the expensive part: a vision model call per
image, a fetch and embedding pass per bookmark, and map-reduce summarisation
for long documents where that is enabled.

When it completes, back up `data\` as a whole. The SQLite file and the Chroma
directory must be copied as a consistent pair; a mismatch between them leaves
chunk rows referring to vectors that no longer exist.

## 11. Upgrading

```bat
schtasks /End /TN Alexandria
robocopy "%LOCALAPPDATA%\Alexandria\data" "%LOCALAPPDATA%\Alexandria\data-backup" /E
cd /d "%LOCALAPPDATA%\Alexandria\app"
git fetch --tags
git checkout v1.1.0
.venv\Scripts\activate
pip install .
cd frontend && npm ci && npm run build && cd ..
alexandria init
schtasks /Run /TN Alexandria
```

Take the backup before anything else. `alexandria init` is idempotent and does
migrate a populated database in place rather than only creating absent tables:
`init_db` in `pka/db/queries.py` runs `create_all`, then a sequence of guarded
`ALTER TABLE` steps for the columns added since the archive was built. What the
backup covers is the case it cannot — a schema change for which no migration
step was written.

Read `CHANGELOG.md` for the release being installed. Configuration keys are
occasionally removed, and because unknown `ALEXANDRIA_*` keys are rejected
rather than ignored, a key left in `.env` after such a change will prevent
startup.

## 12. Uninstalling

```bat
schtasks /Delete /TN Alexandria /F
rmdir /s /q "%LOCALAPPDATA%\Alexandria\app"
```

Delete the shortcuts from the Desktop and from
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Alexandria\`.

`%LOCALAPPDATA%\Alexandria\data` is left in place deliberately, since it holds
the library. Remove it separately once you are certain it is no longer wanted.
