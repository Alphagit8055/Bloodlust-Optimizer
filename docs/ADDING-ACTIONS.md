# Adding an action

Two files. A handler in Python, and an entry in the catalog. The handler must
ship before the catalog entry can reference it.

---

## 1. Write the handler

Add it to the matching module under `sidecar/src/bloodlust/actions/`.

```python
from ..core import registry as reg
from ..core.result import ActionResult
from .base import action


@action(
    args={
        "value": {"type": int, "min": 0, "max": 100, "default": 10},
    },
    doc="One line for the developer docs.",
)
def set_something(ctx, args) -> ActionResult:
    result = ActionResult(action_id=ctx.action_id)

    change = ctx.reg.set_value(
        "HKLM", r"SOFTWARE\Example", "Value", reg.REG_DWORD, int(args["value"]),
        describe="example value",
    )
    if change:
        result.add(change)
        result.message = f"value set to {args['value']}"
    else:
        result.message = "value was already correct"

    return result


HANDLERS = {
    # ...existing entries...
    "example.set_something": set_something,
}
```

Then register the module in `actions/__init__.py` if it is a new file.

### Rules that are not optional

**Never touch `winreg` directly.** Use `ctx.reg`. It reads before it writes,
records the previous value in the undo journal, and short-circuits when the
value is already correct. Bypassing it produces a change with no undo.

**Never build a command string.** Use `ctx.shell.run([...])` with an argv list.
There is no `shell=True` path in this codebase and there should not be one.

**Return something the executor understands.** An `ActionResult`, a
`list[Change]`, a `str` message, or `None`. Anything else is a hard error at
runtime — deliberately, so a typo surfaces immediately instead of silently
doing nothing.

**Declare your args.** The `@action(args=...)` schema is what validates the
catalog's values. Undeclared args are rejected, so a remote catalog cannot
widen what your handler accepts.

---

## 2. Add the catalog entry

In `catalog/catalog.json`:

```json
{
  "id": "example_set_something",
  "label": "Set the example value",
  "family": "windows",
  "risk": "moderate",
  "handler": "example.set_something",
  "args": { "value": 10 },
  "requires": { "platform": "win32", "elevated": true },
  "description": "Explain what it does and what it costs.",
  "tags": ["example"]
}
```

Pick `risk` honestly:

| Level | Meaning |
|---|---|
| `safe` | Read-only, cosmetic, or trivially reversible with no downside |
| `moderate` | Reversible, but changes real behaviour (power plan, scheduling) |
| `aggressive` | Reversible, with a genuine trade-off (heat, battery, security) |
| `danger` | Weakens a security boundary, or is one-way |

**Anything `aggressive` or above must set `"default_enabled": false`.** There is
a test that fails the build otherwise — it exists so a batch run can never
apply an aggressive action the user did not choose.

---

## 3. Test it

Add a test to `sidecar/tests/test_actions.py`:

```python
def test_set_something_writes_the_value(executor, backend):
    executor.run("example_set_something")
    assert backend.read("HKLM", r"SOFTWARE\Example", "Value") == (reg.REG_DWORD, 10)


def test_set_something_undoes(executor, backend):
    executor.run("example_set_something")
    executor.undo("example_set_something")
    assert backend.read("HKLM", r"SOFTWARE\Example", "Value") is None
```

The `executor` fixture gives you a real executor against an in-memory registry
with a fake shell, so no Windows and no admin rights are needed.

Then:

```bash
cd sidecar && python -m pytest tests -q
```

---

## 4. Verify the catalog

CI does this, but do it locally before pushing:

```bash
cd sidecar
python -c "
import sys; sys.path.insert(0,'src')
from bloodlust import actions
from bloodlust.core import catalog as cm
cat = cm.load_file('../catalog/catalog.json', actions.allowlist())
print(len(cat.actions), 'actions OK')
"
```

If it raises `CatalogError`, the message lists every problem at once.

---

## What happens on the user's machine

Editing `catalog/catalog.json` on `main` is live for every user on their next
launch — no rebuild, no reinstall. But the handler has to already be in the
binary they are running.

So the order matters:

1. Write the handler, merge it, **publish a release**
2. *Then* add the catalog entry

If you do it the other way round, every installed app rejects the catalog and
falls back to its bundled copy. It fails safely — the app keeps working, just
without the new action — but it will look like your change did nothing.

---

## Handlers that need to run a command

Use the injectable shell. In a test, `fake_shell.responses` maps a substring of
the argv to canned output:

```python
def test_uses_powercfg(executor, fake_shell):
    fake_shell.responses["/getactivescheme"] = (
        0, "Power Scheme GUID: 381b4222-... (Balanced)", "")
    result = executor.run("power_scheme_performance")
    assert result["ok"]
    assert any("/setactive" in " ".join(c) for c in fake_shell.all_calls)
```

For `powercfg` specifically, use `PowerCfg` from `actions/base.py` rather than
calling it directly — it reads the current value, journals the inverse, and
re-applies the scheme so the change actually takes effect. Getting that last
part wrong is the single most common `powercfg` mistake.

## Handlers that touch ADB

Only settings in `actions/adb.ALLOWED_SETTINGS` can be changed, and only with
values from that table. If you need a new key, add it there — but think about
whether it belongs. The guest namespace is wide and some of it is genuinely
risky, so the allowlist is short on purpose.
