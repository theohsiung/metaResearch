---
name: meta-research-setup
description: Find out which compute a meta-research run may use before it starts — probe this machine and any ssh hosts (GPUs, memory, disk, docker, model endpoints), then ask the user, one question at a time, what a probe cannot see (other machines, when each is free, services that must be back by a deadline, shared-endpoint limits, booking limits, which actions need their OK) — and record it in resources.json + resources.md. Use when starting a new meta-research run, when the experiment dir has no resources.json, when the user adds, books or loses a machine, or before scheduling expensive runs on hardware not yet checked.
---

# meta-research-setup

The research loop has to know where it may run before it runs anything. A probe can tell you what a machine *has*;
only the user can tell you what you may *use*, and when. This skill gathers both and writes them down once, so
every later iteration reads `resources.json` instead of guessing.

Run it from the experiment directory (the one holding `prepare.py`). Re-run it whenever the hardware changes.

## 1. Probe what you can see

```
meta-research resources probe --path <data dir> --path <checkpoint dir>     # this machine
meta-research resources probe --endpoint http://localhost:8000               # plus any model endpoints you find
```

Read `program.md` and `prepare.py` first: they tell you which paths the run needs (models, data, checkpoints) and
roughly how heavy one evaluation is (`EVAL_MINUTES`). Look for running services yourself (for example
`ss -ltnp`, `docker ps`) before asking about them.

## 2. Ask the user what a probe cannot see

Ask **one question at a time** with AskUserQuestion. Offer concrete options, put the default you recommend first,
and never ask what a probe or a file already answered. Stop asking once each item below is either settled or
clearly not relevant. In order:

1. **Other machines.** Are there machines besides this one (ssh hosts, a cluster, booked cloud GPUs)? How do you
   reach each (`user@host`, a key that must be installed first)?
2. **When each is free.** Always, only at night, only on some days, until a booking ends? Get times as HH:MM in
   the user's local time zone.
3. **Deadlines.** Must anything be restored by a set time (a shared inference service, a colleague's job)? Agree a
   stop time with enough margin to save a checkpoint (for example stop at 07:53 for an 08:00 deadline).
4. **Shared services.** Endpoints the run may call (a teacher model, an API): their URL, the concurrency limit,
   and when they are up.
5. **Cost and booking.** Paid hours, quotas, or a date when a booked machine goes away.
6. **Environment.** Must runs go through a container or a specific env? Which image, which launcher script?
7. **Paths.** Where data, models and checkpoints live on each host; which must exist before a run.
8. **What needs a yes first.** Actions the user wants to approve (uploads, scheduling on shared GPUs, using a
   second machine, deleting checkpoints).

## 3. Probe and check every host

```
meta-research resources probe --host user@host --path <data dir>
```

Write a draft `resources.json` (schema below), then run `meta-research resources check`. It verifies on every host
that `required_paths` exist, that `executables` keep their executable bit (a copied launcher loses it), and
that `min_free_gb` holds. Fix what fails (sync the missing paths, `chmod +x`) and re-run until it passes. List
the paths a run reads, not only the top directory: a copy that broke off leaves the directory but not its files.

## 4. Write it down and show it

`resources.json`, version 1:

```json
{"version": 1,
 "hosts": [{"name": "local", "ssh": null,
            "accelerators": [{"vendor": "nvidia", "name": "...", "memory_gb": 96}],
            "container": null,
            "windows": [{"days": "all", "start": "00:30", "end": "07:53", "note": "GPU serves vLLM by day"}],
            "deadlines": [{"time": "08:00", "what": "vLLM teacher back up", "stop_at": "07:53"}],
            "required_paths": ["/data/models/base/config.json"], "executables": [], "min_free_gb": 100,
            "notes": ""},
           {"name": "mi300x-1", "ssh": "root@10.0.0.5", "accelerators": [...],
            "container": {"image": "proj-rocm", "run": "/root/proj/run.sh"},
            "windows": [], "deadlines": [], "required_paths": [...], "executables": ["/root/proj/run.sh"],
            "min_free_gb": 200, "notes": "booked until Nov 30"}],
 "services": [{"name": "teacher", "url": "http://localhost:8000", "max_concurrency": 4,
               "windows": [], "notes": "shared with other users"}],
 "confirm_before": ["uploads", "using the second machine"]}
```

- No `windows` means always available. A window whose end is before its start crosses midnight, and its `days`
  (`"all"` or a list of `mon`..`sun`) name the day it starts.
- Copy `accelerators` from the probe.
- `resources.md`: a short human summary of the same facts, plus anything that does not fit the schema.

Run `meta-research resources show`, show the user both files, and ask them to correct anything wrong. Then commit
both files with the ledger.

## 5. Use it in the loop

- Before launching an expensive evaluation, run `meta-research resources show` and `meta-research resources check`.
  Run only on a host that is available now and has passed its check. Make sure the run ends, or checkpoints,
  before that host's next stop time.
- Respect each service's `max_concurrency`. Ask before anything listed in `confirm_before`.
- When the user mentions a new machine, a lost booking or a changed schedule, update `resources.json`, re-run
  `check` and commit.
