# ClockIt — Copilot Instructions

## Stack & Environment

- **Backend**: FastAPI + SQLAlchemy + Alembic + PostgreSQL (Python 3.12, `.venv`)
- **Frontend**: React + Vite + Recharts + Playwright (port 5173, proxy to backend port 8004)
- **Dev DB**: `clockit-db-dev` container, port 5434 — `DATABASE_URL` in `.env`
- **Test DB**: `clockit-db-test` container, port 5435 — `TEST_DATABASE_URL` in `.env.test`
- **Live DB**: EC2 `awsdocker.jgf.ie` via Docker context `jgf-vm`
- **Theme**: `ThemeContext.jsx` sets `data-theme` on `document.documentElement`; CSS vars in `src/styles/theme.css`

See `docs/development/STARTUP_GUIDE.md` for startup commands and `docs/testing/TESTING.md` for test setup.

---

## Programming Style

### Functional-first; classes only for data objects

Write functions, not classes, for all services, handlers, helpers, and utilities. Classes are
**only** appropriate for data/value objects (e.g. Pydantic models, SQLAlchemy ORM models). Never
wrap logic in a class just to organise it — use modules and plain functions instead.

```python
# ✅ correct
def compute_billable_hours(entries: list[TimeEntry]) -> float: ...

# ❌ wrong — this is a service, not a data object
class BillableHoursCalculator:
    def compute(self, entries): ...
```

### Frontend code must be verbose and explained

When writing or editing React/JS code, explain **what each non-trivial piece does and why**,
especially:
- Hooks (`useState`, `useEffect`, `useMemo`, `useCallback`) — state each one's trigger and purpose
- Custom hook dependencies
- Side-effect cleanup
- Any Recharts API quirks (e.g. why CSS vars don't work in SVG attributes)

The goal is that the user can read and understand every change without needing to ask follow-up
questions.

---

## Backend Changes — Permission Required First

**Never write backend code first.** When a task involves backend changes:

1. Describe what needs to happen (endpoints added/changed, schema changes, new logic)
2. Explain why that approach was chosen
3. Ask for explicit permission before writing any code

Frontend-only changes can proceed without permission.

---

## Terminal Commands

### Never truncate output with `| tail` or `| head`

Always capture full output to a file and read it from there:

```bash
# ✅ correct
command 2>&1 | tee output.log
# then read output.log to check results

# ❌ wrong — hides failures above the cut-off
command 2>&1 | tail -20
```

### Playwright tests

Always run with `tee` and read the full log:

```bash
cd react-frontend
npx playwright test 2>&1 | tee frontend_test.log
# read frontend_test.log — never tail it
```

When fixing test failures: read the **full** error block in the log file, not just the summary
line. Performance thresholds must be set per-browser because WebKit and Firefox are significantly
slower in headless mode than Chromium.

---

## Alembic / Database

- Alembic owns the **full** migration chain from `0000_initial_schema` (no `create_tables()` bootstrap)
- Never add `NOT NULL` columns without a `server_default` or nullable flag
- Live DB is at migration `20260125_add_data_cleanup`; new migrations run on next deploy safely
- Test DB credentials: `shifting5279` / (see `.env.test`) — test user has `onboarding_completed=True`

---

## CSS / Theming

Recharts renders SVG attributes — CSS custom properties are **not** resolved by the browser in
SVG context. Always resolve them at render time:

```js
// ✅ correct — resolved value passed to Recharts
const color = getComputedStyle(document.documentElement).getPropertyValue('--primary-500').trim()

// ❌ wrong — SVG ignores CSS vars
<Line stroke="var(--primary-500)" />
```

Use the `MutationObserver` pattern (not `useMemo([theme])`) to re-read CSS vars after the
`data-theme` attribute is updated, because `useEffect` in ThemeContext runs after render.
