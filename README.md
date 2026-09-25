# dava

Command-line interface for DaVinci Resolve, built on Resolve's external scripting API
(`DaVinciResolveScript`). No third-party runtime dependencies.

## Requirements

- DaVinci Resolve running, with Preferences > System > General > External scripting using = Local.
  (The Resolve README documents this preference for Resolve Studio.)
- Python 3.10+. The SDK module is found automatically at the default install location; otherwise set
  `RESOLVE_SCRIPT_API` (Developer/Scripting folder) and, if needed, `RESOLVE_SCRIPT_LIB` (fusionscript library).

## Install

    uv sync
    uv run dava --help

or install as a tool: `uv tool install .`

## Commands

    dava status                                   # product, version, page, project, timeline
    dava page [media|cut|edit|fusion|color|fairlight|deliver|photo]
    dava project list | open NAME | create NAME | save | settings [KEY...]
    dava timeline list | use NAME | create NAME
    dava timeline export OUT -f {edl,aaf,fcpxml,otio,drt,csv,...} [-t TIMELINE]
    dava media import PATH... [--append]
    dava media list [--all]
    dava marker list [-t TIMELINE]
    dava marker add FRAME [-c COLOR] [-n NAME] [--note TEXT] [-d FRAMES] [-t TIMELINE]
    dava render presets | jobs | stop
    dava render add [-p PRESET] [-o DIR] [-n NAME] [-t TIMELINE | --all-timelines]
    dava render start [JOB_ID...] [--wait]

Color and Fusion commands act on clips of a timeline video track. Select them with
`-t TIMELINE` (default: current), `-T TRACK` (default: 1) and `-i INDEX` (clip number from 1;
omit to act on every clip of the track where the command allows it).

    dava color nodes                              # nodes, labels, LUTs, tools per clip
    dava color lut PATH [-N NODE]
    dava color cdl [--slope 'R G B'] [--offset 'R G B'] [--power 'R G B'] [--saturation S] [-N NODE]
    dava color drx FILE [-k none|source-tc|start-frames]
    dava color reset
    dava color export-lut OUT -i INDEX [-s 17|33|65|vlut]
    dava still list [-a ALBUM] | grab [--all first|middle] | export DIR [-a ALBUM] [-f FORMAT] [--prefix P]

    dava meta get CLIP [KEY...] [--third-party]   # media pool clip, found by name in any folder
    dava meta set CLIP KEY=VALUE... [--third-party]
    dava meta props CLIP [KEY...]
    dava meta set-prop CLIP KEY=VALUE...
    dava meta color CLIP [COLOR|none]

    dava fusion list
    dava fusion add | import FILE.comp | export OUT [-c N] | delete NAME | rename OLD NEW   (-i INDEX required)

## Headless and batch use

    dava launch --nogui            # start Resolve without UI, wait until scripting answers
    dava batch jobs.dava           # run one dava command per line over a single connection
    dava quit

A batch file holds one command per line (without the `dava` prefix); `#` starts a comment. Every
line is parsed before any runs. It stops at the first failure unless `--keep-going` is given. With
`--json` each command prints one line of JSON.

    # jobs.dava
    project open Promo
    render add --all-timelines -p "H.264 Master" -o /renders
    render start --wait
    quit

`launch` finds Resolve at the default install path; set `RESOLVE_APP` to override it.

Add `--json` before the command for machine-readable output. Exit code 1 means Resolve reported an
error (message on stderr); 2 means a usage error.

## The whole API, for scripts and AI agents

Every method of the Resolve scripting API (410 in Resolve 21.1) is reachable through three commands.
They read the API definition that ships with Resolve (`Developer/Scripting/DaVinciResolveScript.pyi`),
so they always match the installed version.

    dava api classes | methods CLASS | show NAME | search WORDS... | constants [PREFIX] | refs | dump
    dava inspect REF
    dava call REF METHOD [VALUE | NAME=VALUE | NAME:=JSON ...] [--args JSON] [--kwargs JSON]
                         [--dry-run] [--as NAME] [--unchecked]

A REF names a live object (`dava api refs` has the full grammar):

    timeline  timeline:NAME  timeline#ID  timeline@N      clip:NAME  clip:/Master/Sub/NAME  clip#ID
    item  item:video:1:3  item#ID    folder:/Master/Sub  folder#ID    colorgroup:NAME   album:NAME
    project  mediapool  mediastorage  gallery  resolve  fusion      children joined with '::', e.g.
    timeline:Edit v1::item:video:1:3::graph     colorgroup:Warm::postgraph     album:Stills 1::still:2

Arguments are checked and converted against the declared parameter types: object parameters take REFs,
`resolve.*` constants take their names, typed dicts take JSON objects. Object results come back as
`{"$ref": ..., "$type": ...}` and can be passed straight back in.

    dava call timeline AddMarker 120 Red "Fix" "" 1
    dava call timeline Export /tmp/cut.edl EXPORT_EDL EXPORT_NONE
    dava call mediapool AppendToTimeline '[{"mediaPoolItem": "clip:a.mov", "startFrame": 0, "endFrame": 48}]'
    dava call item:video:1:2::graph SetLUT 1 "Film Looks/Rec709 Kodak 2383 D65.cube"
    dava call mediastorage StartCloneMedia /Volumes/CARD_A '["/Volumes/RAID/A", "/Volumes/BACKUP/A"]'

`call` prints JSON and exits 1 when a state-changing method returns False. In a batch file, `--as g` keeps a
result as `$g` for later lines. Objects without a stable id (graphs, stills, Fusion comps) come back as
`$hN` handles that only live for one batch or MCP session.

### Read-only mode

`--read-only` (or `DAVA_READ_ONLY=1`) refuses every command and API method that can change Resolve:
only `Get*`/`Is*`/`Has*` methods and the listing commands run.

### MCP server

`dava mcp` serves these tools over stdio for MCP clients such as Claude Code:

- `resolve_api_search` and `resolve_api_describe`: explore the API
- `resolve_inspect`: show what a REF points at
- `resolve_get`: call a read-only method
- `resolve_call`: call any method
- `dava_command`: run any dava CLI command

With `--read-only`, `resolve_call` is not offered.

    claude mcp add --scope project dava -- uv run --project /path/to/dava dava mcp
    claude mcp add dava-readonly -- uv run --project /path/to/dava dava mcp --read-only

or in a project `.mcp.json`:

    {"mcpServers": {"dava": {"type": "stdio", "command": "uv",
                             "args": ["run", "--project", "/path/to/dava", "dava", "mcp"]}}}

## Tests

    uv run pytest
