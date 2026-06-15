# Sortable Tables — Reuse Pattern

## How to Add Sorting to Any Table Listing Page

Add these two pieces to your page template:

### 1. Table Structure

Use `<thead>` and `<tbody>` — the sort JS only sorts `<tbody>` rows:

```html
<table>
<thead>
  <tr>
    <th class="sortable" data-col="0">ID</th>
    <th class="sortable" data-col="1">Name</th>
    <th class="sortable" data-col="2">Quant</th>
    <th class="sortable" data-col="3">Model Path</th>
    <th class="sortable" data-col="4">MMPROJ Path</th>
    <th class="sortable" data-col="5">Adapter Path</th>
    <th class="sortable" data-col="6">Size</th>
    <th>Actions</th>  <!-- no sortable class = not sortable -->
  </tr>
</thead>
<tbody>
  <!-- data rows here -->
</tbody>
</table>
```

- `data-col="N"` — zero-based column index to sort by
- Arrow indicators (↑/↓) auto-appear on click
- `qrSettings` (localStorage) persists sort state across page reloads and browser restarts
  - Pattern: `qrSettings.get('page', 'sort_col/dir', ...)`
  - All pages use the same centralized store (see `webui/base.html`)
- **Always include an ID column as col 0** — enables quick reference and numeric sorting

### 2. JavaScript

Include this script block at the bottom of your page:

```javascript
<script>
// Column sorting — uses qrSettings (localStorage) for persistent state
var ths = document.querySelectorAll('.sortable');
ths.forEach(function(th) {
  th.style.cursor = 'pointer';
  th.addEventListener('click', function() {
    var col = parseInt(this.getAttribute('data-col'), 10);
    var currentDir = qrSettings.get('page_name', 'sort_dir', null);
    var dir = currentDir === 'desc' ? 'asc' : 'desc';
    qrSettings.set('page_name', 'sort_dir', dir);

    // Update arrows
    ths.forEach(function(t) {
      t.textContent = t.textContent.replace(/\s*[\u2191\u2193]\s*$/, '').trim();
    });
    var arrow = dir === 'asc' ? ' \u2191' : ' \u2193';
    this.textContent = this.textContent.trim() + arrow;

    // Sort tbody rows in place
    var table = this.closest('table');
    if (table) {
      var tbody = table.querySelector('tbody');
      if (tbody) {
        var rows = Array.from(tbody.rows);
        rows.sort(function(a, b) {
          var cellA = a.cells[col] ? a.cells[col].textContent.trim() : '';
          var cellB = b.cells[col] ? b.cells[col].textContent.trim() : '';
          // Numeric sort: ID(0), Usage, Errors, Size, Duration, Prompt/s, Pred/s
          if (col === 0 || col === 5 || col === 6) { // adjust per page
            return dir === 'asc' ? (parseFloat(cellA.replace(/[^0-9.]/g, '')) || 0) - (parseFloat(cellB.replace(/[^0-9.]/g, '')) || 0) : (parseFloat(cellB.replace(/[^0-9.]/g, '')) || 0) - (parseFloat(cellA.replace(/[^0-9.]/g, '')) || 0);
          }
          return dir === 'asc' ? cellA.localeCompare(cellB) : cellB.localeCompare(cellA);
        });
        rows.forEach(function(row) { tbody.appendChild(row); });
      }
    }
  });
});
</script>
```

### Notes

- ID column (col 0) is always numeric sort — clean numeric comparison for reference
- Size/Duration/Prompt/s/Pred/s columns use numeric sort after stripping units
- Timestamp columns (created_at, last_state_change): display as relative time via `fmtRelative()` JS function, sort via `data-sort` attribute with raw ISO for chronological ordering
- All other columns use string `localeCompare` sorting
- Sort state stored in `qrSettings` localStorage namespace (not per-column sessionStorage keys)
- Arrow characters: ↑ (U+2191) ascending, ↓ (U+2193) descending

## AGE and Last Info Columns — Relative Time Format

All list views use **relative time formatting** (`fmtRelative()`) for age/created_at and last_state_change columns. Server renders raw ISO timestamp in both display text and `data-sort` attribute (for sorting); JS converts display to relative format on load.

### Format Spec

| Age Range | Output Format | Examples |
|-----------|--------------|----------|
| >= 7 days | `<N>d` | `10d`, `30d` |
| 1d – 6d23h | `<N>d <H>h` | `2d 5h`, `6d 23h` |
| 1h – 23h59m | `<N>h <M>m` | `3h 45m`, `23h 59m` |
| 1m – 59m59s | `<N>m <S>s` | `34m 12s`, `59m 45s` |
| < 1 minute | `<N>s` | `12s`, `55s` |

No "ago" suffix. No full word extensions. Coarse for old, precise for recent.

### Server-side: Use `created_at` with `data-sort` attribute

```html
<!-- GOOD — raw ISO in display + data-sort (JS converts display to relative) -->
<td class="age-cell" data-sort="{{ inst.created_at or '' }}">{{ inst.created_at or '-' }}</td>
```

For Last Info column:
```html
<td class="last-info-cell" data-ts="{{ inst.last_state_change or '' }}" data-id="{{ inst.id }}">
  {{ inst.last_state_change[:19] if inst.last_state_change else '-' }}
  <button class="btn btn-xs refresh-inst" data-id="{{ inst.id }}">&#8635;</button>
</td>
```

### Client-side: Add `fmtRelative()` and apply on load

```javascript
// Format ISO timestamp as relative time: 10d, 6d 14h, 23h 30m, 34m 12s, 55s
function fmtRelative(iso) {
    if (!iso || iso === '-') return '-';
    var now = new Date();
    var ts = new Date(iso);
    if (isNaN(ts.getTime())) return iso;
    var diff = Math.floor((now - ts) / 1000);
    if (diff < 0) return iso;
    var d = Math.floor(diff / 86400);
    diff %= 86400;
    var h = Math.floor(diff / 3600);
    diff %= 3600;
    var m = Math.floor(diff / 60);
    var s = diff % 60;
    if (d >= 7) return d + 'd';
    if (d > 0) return d + 'd ' + h + 'h';
    if (h > 0) return h + 'h ' + m + 'm';
    if (m > 0) return m + 'm ' + s + 's';
    return s + 's';
}
// Apply on DOM load
(function() {
  document.querySelectorAll('.age-cell').forEach(function(cell) {
    var raw = cell.getAttribute('data-sort') || cell.textContent.trim();
    cell.textContent = fmtRelative(raw);
  });
})();
```

For dynamic (JS-rendered) pages, call `fmtRelative()` after row rendering:
```javascript
document.getElementById('models-tbody').innerHTML = rows;
document.querySelectorAll('#models-tbody .age-cell').forEach(function(cell) {
  var raw = cell.getAttribute('data-sort') || cell.textContent.trim();
  cell.textContent = fmtRelative(raw);
});
```

For Last Info cells (preserve button HTML):
```javascript
document.querySelectorAll('.last-info-cell').forEach(function(cell) {
  var raw = cell.getAttribute('data-ts') || cell.textContent.trim();
  var id = cell.getAttribute('data-id');
  var btnHtml = '<button class="btn btn-xs refresh-inst" data-id="' + (id||'') + '" title="Query remote status" style="font-size:0.7em;padding:2px 5px;">&#8635;</button>';
  cell.innerHTML = fmtRelative(raw) + btnHtml;
});
```

## Column Sortability Matrix

All list view pages should sort by: ID, Name, Engine/Type, Node/Host, State, Age (created_at). Additional sortable columns vary by page:

| Page | Sortable Columns |
|------|-----------------|
| instances.html | 0-ID, 1-Name, 2-Age, 3-Engine, 4-Node, 5-Remote, 6-Preset, 7-State, 8-Last Info |
| hosts.html | 0-ID, 1-Name, 2-Age, 3-Hostname, 4-Instances, 5-Capabilities, 6-Status |
| models.html / engine_models.html | 0-ID, 1-Name, 2-Age, 3-Quant, 4-Model Path, 5-MMPROJ, 6-Adapter, 7-Size |
| presets.html (per-engine) | 0-ID, 1-Name, 2-Age, 3-Category |
| playbooks.html | 0-ID, 1-Playbook ID, 2-File Path, 3-Type, 4-Version, 5-Usage, 6-Errors, 7-Age |
| ansible_logs.html | 0-ID, 1-Timestamp, 2-Action, 3-Node, 4-Instance, 5-Status |
| iperf3.html (JS-rendered) | 0-ID, 1-Name, 2-Age, 3-Category |
| benchmark.html (JS-rendered) | 0-Time, 1-Instance, 2-Prompt, 3-Model, 4-Duration, 5-Prompt/s, 6-Pred/s |
| rpccluster.html (JS-rendered) | Dynamic — columns not sortable by default |

## Numeric Sort Patterns Reference

Different pages use different numeric column indices. Always verify:

```javascript
// Pattern 1: ID column is 0
if (col === 0) { return numericSort(dir, cellA, cellB); }

// Pattern 2: ID column doesn't exist, first numeric column varies
if (col === 5 || col === 6) { return numericSort(dir, cellA, cellB); } // Usage/Errors or Size/Duration

// Pattern 3: Per-page custom indices — always check data-col values
```

Use helper for cleaner code:
```javascript
function numSort(a, b, dir) {
  var na = parseFloat(a.replace(/[^0-9.-]/g, '')) || 0;
  var nb = parseFloat(b.replace(/[^0-9.-]/g, '')) || 0;
  return dir === 'asc' ? na - nb : nb - na;
}
```

## Async Data Loading Gotcha (2026-06-09 Fix)

When using `qrApi()` for **dynamic content loading** (server-rendered tbody placeholder replaced by JS), the sort initialization MUST run **after** tbody rows are populated, NOT in the synchronous page-init block.

### The Bug

```javascript
// WRONG — runs before fetch completes, tbody has 0 rows
loadModels(); // fire-and-forget fetch

var ths = document.querySelectorAll('.sortable');
ths.forEach(function(th) { th.addEventListener('click', ...); });
// If page-load auto-sort is needed here: operates on empty tbody

// Arrow indicators still show (they target static <th> elements)
// But tbody rows are not sorted — silent failure
```

**Symptoms:** Arrow shows on column header after click, but table not sorted on page refresh. Arrows operate on `<th>` elements (always present). Sort operates on `tbody` rows (0 until fetch `.then()` callback populates them).

### The Fix

```javascript
// CORRECT — sort runs inside fetch .then() AFTER tbody.innerHTML = rows
function loadModels() {
  fetch(QR_API_BASE + '/models').then(function(r) {
    return r.json();
  }).then(function(data) {
    document.getElementById('models-tbody').innerHTML = rows;

    // Apply saved sort state (after tbody populated)
    var _sortCol = parseInt(qrSettings.get('models', 'sort_col', '-1'), 10);
    var _sortDir = qrSettings.get('models', 'sort_dir', '');
    if (_sortCol >= 0 && _sortDir) {
      modelsSortTable(_sortCol, _sortDir);
      modelsApplyArrows(_sortCol, _sortDir);
    }
  });
}

// Click handlers can register synchronously — they will call sort functions
// which operate on whatever tbody content exists at click time (correct behavior)
var ths = document.querySelectorAll('.sortable');
ths.forEach(function(th) {
  th.addEventListener('click', function() {
    // ... update qrSettings, call modelsSortTable(), modelsApplyArrows()
    // At this point tbody already has rows from initial load
  });
});
loadModels(); // fire after handlers registered
```

### Checklist for Pages Using Dynamic Content Loading

Apply these criteria when implementing sorting on dynamically-rendered pages:

1. **Are rows populated by JS (not server-rendered)?** If yes, sort init must be inside the fetch `.then()` callback.
2. **Does the page need auto-apply saved sort state on load?** Yes — call `sortTable()` and `applyArrows()` after tbody population in the same `.then()` block.
3. **Click handlers registered synchronously?** Fine — they operate on whatever content exists at click time. No change needed.
4. **Arrow indicators?** They target `<th>` elements which are always present. Always show correctly regardless of data timing.

### Pages Requiring This Pattern (Dynamic Render)

| Page | Current State | Needs Fix? |
|------|--------------|------------|
| `models.html` | Fixed — sort in `.then()` after tbody population | Done |
| `instances.html` | Server-rendered with JS refresh | Likely fine (initial load has rows) |
| `playbooks.html` | Server-rendered with JS actions | Likely fine |
| `hosts.html` | Server-rendered | Likely fine |
| `ansible_logs.html` | Server-rendered | Likely fine |
| `rpccluster.html` | JS-rendered, no sorting yet | Not applicable (no sort implemented) |
| `iperf3.html` | JS-rendered, no sorting yet | Not applicable |
| `benchmark.html` | JS-rendered, no sorting yet | **Needs fix** when sorting is added |

### Pattern Summary

- **Server-rendered pages:** Sort init in synchronous block (tbody already has rows)
- **JS-rendered pages (qrApi fetch):** Sort init inside `.then()` callback after `tbody.innerHTML = rows`
- **Click handlers:** Always register synchronously — they work at click time regardless
- **Arrow indicators:** Target `<th>` — always visible, never affected by data timing
