/* Benchmark page - external JavaScript module */
(function() {
  const API_BASE = "/api/v1";
  let currentRunId = null;
  const promptsData = window.__BENCH_CONFIG__.prompts;
  const presetsForLlama = window.__BENCH_CONFIG__.llama_presets || [];

  // Benchmark result state constants
  const BENCH_SUCCESS = 1;
  const BENCH_FAILED = -1;
  const BENCH_RUNNING = 0;

  // Track active runs by instance_id: {instance_id: {run_id, started_at}}
  let activeRuns = {};
  let autoRefreshInterval = null;

  // --- Instance selector: show current preset/model info (no dropdown clear) ---
  document.getElementById("instance-select").addEventListener("change", function() {
    let instId = this.value;
    let applyBtn = document.getElementById("apply-preset-btn");
    let infoEl = document.getElementById("preset-info");

    applyBtn.disabled = !instId;
    infoEl.textContent = '';

    if (!instId) return;

    fetch(API_BASE + "/instances/" + instId)
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.status !== "ok") return;
        let inst = data.data || {};

        // Select current preset in dropdown if it matches a server-rendered option
        if (inst.preset_id) {
          let presetSel = document.getElementById("preset-select");
          for (let i = 0; i < presetSel.options.length; i++) {
            if (parseInt(presetSel.options[i].value) === inst.preset_id) {
              presetSel.selectedIndex = i;
              break;
            }
          }
        }

        // Show current model info
        let env = inst.merged_config || inst.ansible_vars || {};
        if (typeof env === 'string') { try { env = JSON.parse(env); } catch(e){} }
        let modelPath = '';
        if (env.env && typeof env.env === 'object') {
          modelPath = env.env.LLAMA_ARG_MODEL || '';
        } else if (env.model) {
          modelPath = env.model.path || env.model.name || '';
        }
        if (modelPath) {
          let modelName = modelPath.split('/').pop();
          infoEl.textContent = 'Current model: ' + modelName;
        }
      });
  });

  // --- Preset change handler ---
  document.getElementById("preset-select").addEventListener("change", function() {
    document.getElementById("apply-preset-btn").disabled = !this.value;
  });

  document.getElementById("apply-preset-btn").addEventListener("click", function() {
    let instId = document.getElementById("instance-select").value;
    let presetVal = this.previousElementSibling.value;
    let newPreset = presetVal ? parseInt(presetVal) : null;
    if (!instId) { alert("Select an instance"); return; }
    if (newPreset === null && newPreset !== 0) return;

    if (newPreset) {
      let engineType = "llama_server";
      fetch(API_BASE + "/engine/" + encodeURIComponent(engineType) + "/presets/" + newPreset)
        .then(function(r) { return r.json(); })
        .then(function(data) {
          let p = data.data || {};
          let details = [];
          details.push('Switch to preset ' + p.id + ': "' + (p.name || '') + '"');
          // Fetch model details if preset references a model
          if (p.model_id) {
            return fetch(API_BASE + "/engine/" + encodeURIComponent(engineType) + "/models/" + p.model_id)
              .then(function(r) { return r.json(); })
              .then(function(modelData) {
                let m = modelData.data || {};
                if (m.id && m.name) {
                  var sizeStr = '';
                  if (m.size_bytes !== undefined && m.size_bytes !== null) {
                    var gb = (m.size_bytes / (1024*1024*1024));
                    sizeStr = ' ' + gb.toFixed(2) + ' GB';
                  }
                  details.push('Model ' + m.id + ': "' + m.name + '" ' + (m.quantization || '') + sizeStr);
                }
                let instName = '';
                if (instId) {
                  var tr = document.querySelector('tr[data-inst-id="' + instId + '"]');
                  if (tr) instName = tr.getAttribute('data-inst-name') || '';
                }
                details.push('On Instance: ' + instName);
                if (p.category) details.push('Category: ' + p.category);
                let msg = details.join('\n');
                if (!confirm(msg)) return;
                doPresetChange(instId, newPreset);
              });
          } else {
            let instName = '';
            if (instId) {
              var tr2 = document.querySelector('tr[data-inst-id="' + instId + '"]');
              if (tr2) instName = tr2.getAttribute('data-inst-name') || '';
            }
            details.push('On Instance: ' + instName);
            if (p.category) details.push('Category: ' + p.category);
            let msg = details.join('\n');
            if (!confirm(msg)) return;
            doPresetChange(instId, newPreset);
          }
        }).catch(function() {
          if (!confirm('Switch to preset #' + newPreset + '?')) return;
          doPresetChange(instId, newPreset);
        });
    } else {
      if (!confirm('Remove preset from this instance? Instance will be re-deployed without a preset.')) return;
      doPresetChange(instId, null);
    }

    function doPresetChange(id, preset) {
      let btn = document.getElementById("apply-preset-btn");
      btn.disabled = true;
      let toast = document.createElement('div');
      toast.style.cssText = 'position:fixed;top:20px;right:20px;z-index:9999;padding:10px 16px;background:#1976d2;color:#fff;border-radius:4px;font-size:0.85em;opacity:0.95;box-shadow:0 2px 8px rgba(0,0,0,0.2);';
      toast.textContent = 'Applying preset change... stopping instance';
      document.body.appendChild(toast);

      fetch(API_BASE + "/instances/" + id, {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({preset_id: preset})
      }).then(function(r) { return r.json(); })
        .then(function(data) {
          toast.style.opacity = '0';
          setTimeout(function() { toast.remove(); }, 300);
          if (data.status === "ok") {
            let t2 = document.createElement('div');
            t2.style.cssText = 'position:fixed;top:20px;right:20px;z-index:9999;padding:10px 16px;background:#388e3c;color:#fff;border-radius:4px;font-size:0.85em;opacity:0.95;box-shadow:0 2px 8px rgba(0,0,0,0.2);';
            t2.textContent = 'Deploying new preset... reloading page';
            document.body.appendChild(t2);
            setTimeout(function() {
              t2.style.opacity = '0';
              setTimeout(function() { t2.remove(); }, 300);
              window.location.reload();
            }, 1500);
          } else {
            btn.disabled = false;
            alert('Preset change failed: ' + (data.message || JSON.stringify(data)));
          }
        })
        .catch(function(e) {
          toast.style.opacity = '0';
          setTimeout(function() { toast.remove(); }, 300);
          btn.disabled = false;
          alert('Request failed: ' + e);
        });
    }
  });

  // --- Save prompt button ---
  document.getElementById("save-prompt-btn").addEventListener("click", function() {
    let nameEl = document.getElementById("prompt-name");
    let contentEl = document.getElementById("prompt-content");
    let maxTokensEl = document.getElementById("prompt-max-tokens");
    let name = nameEl.value.trim();
    let content = contentEl.value.trim();
    if (!name) { alert("Enter a prompt name"); return; }
    if (!content) { alert("Enter prompt content"); return; }

    fetch(API_BASE + "/benchmarks/prompts", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: name, content: content, max_tokens: parseInt(maxTokensEl.value) || 20})
    }).then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.status === "ok") {
          loadPrompts();
          nameEl.value = "";
          contentEl.value = "";
          alert("Prompt saved!");
        } else {
          alert("Save failed: " + (data.message || "Unknown error"));
        }
      }).catch(function(e) { alert("Error: " + e); });
  });

  // --- Load prompts into dropdown ---
  function loadPrompts() {
    fetch(API_BASE + "/benchmarks/prompts")
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.status !== "ok") return;
        let sel = document.getElementById("prompt-select");
        while (sel.options.length > 1) sel.remove(1);
        data.items.forEach(function(p) {
          let opt = document.createElement("option");
          opt.value = p.id;
          opt.textContent = p.name + (p.max_tokens ? " (tokens:" + p.max_tokens + ")" : "");
          sel.appendChild(opt);
        });
      });
  }

  // --- Prompt select change: show edit/delete buttons and populate form ---
  document.getElementById("prompt-select").addEventListener("change", function() {
    let editBtn = document.getElementById("edit-prompt-btn");
    let delBtn = document.getElementById("delete-prompt-btn");
    if (!this.value) {
      editBtn.style.display = "none";
      delBtn.style.display = "none";
      return;
    }
    editBtn.style.display = "";
    delBtn.style.display = "";

    let pid = parseInt(this.value);
    fetch(API_BASE + "/benchmarks/prompts/" + pid)
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.status === "ok") {
          document.getElementById("prompt-name").value = data.data.name || "";
          document.getElementById("prompt-content").value = data.data.content || "";
          document.getElementById("prompt-max-tokens").value = data.data.max_tokens || 20;
        } else {
          alert("Load failed: " + (data.message || "Unknown error"));
        }
      })
      .catch(function(e) { alert("Error loading prompt: " + e); });
  });

  // --- Edit prompt button ---
  document.getElementById("edit-prompt-btn").addEventListener("click", function() {
    let sel = document.getElementById("prompt-select");
    if (!sel.value) return;
    let pid = parseInt(sel.value);
    let name = document.getElementById("prompt-name").value.trim();
    let content = document.getElementById("prompt-content").value.trim();
    let maxTokens = parseInt(document.getElementById("prompt-max-tokens").value) || 20;
    if (!name || !content) { alert("Fill in both fields"); return; }

    fetch(API_BASE + "/benchmarks/prompts/" + pid, {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: name, content: content, max_tokens: maxTokens})
    }).then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.status === "ok") {
          loadPrompts();
          alert("Prompt updated!");
        } else {
          alert("Update failed: " + (data.message || "Unknown error"));
        }
      }).catch(function(e) { alert("Error: " + e); });
  });

  // --- Delete prompt button ---
  document.getElementById("delete-prompt-btn").addEventListener("click", function() {
    let sel = document.getElementById("prompt-select");
    if (!sel.value) return;
    let pid = parseInt(sel.value);
    if (!confirm('Delete prompt "' + sel.options[sel.selectedIndex].textContent + '"?')) return;

    fetch(API_BASE + "/benchmarks/prompts/" + pid, {method: "DELETE"})
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.status === "ok") {
          loadPrompts();
          document.getElementById("prompt-name").value = "";
          document.getElementById("prompt-content").value = "";
          sel.value = "";
          alert("Prompt deleted!");
        } else {
          alert("Delete failed: " + (data.message || "Unknown error"));
        }
      }).catch(function(e) { alert("Error: " + e); });
  });

  // --- Start benchmark button (supports parallel runs on different instances) ---
  document.getElementById("start-benchmark-btn").addEventListener("click", function() {
    let instId = document.getElementById("instance-select").value;
    let promptId = document.getElementById("prompt-select").value;
    if (!instId) { alert("Select an instance"); return; }
    if (!promptId) { alert("Select or create a prompt"); return; }

    // Check if this instance already has a running benchmark
    if (activeRuns[instId]) {
      alert("Benchmark already running on this instance. Select a different instance for parallel runs.");
      return;
    }

    let btn = this;
    let statusEl = document.getElementById("bench-status");
    let outputEl = document.getElementById("output-window");
    btn.disabled = true;
    btn.textContent = "Starting...";
    statusEl.textContent = "";
    outputEl.textContent = "Starting benchmark...\n";

    fetch(API_BASE + "/benchmarks/run", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({instance_id: parseInt(instId), prompt_id: parseInt(promptId)})
    }).then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.status === "ok") {
          let runId = data.data.run_id;
          activeRuns[instId] = {run_id: runId, started_at: new Date().toISOString()};
          statusEl.textContent = "Run ID: " + runId + " | Instance: " + instId;
          startPolling(instId, runId);
          btn.disabled = false;
          btn.textContent = "Start Benchmark";
        } else {
          btn.disabled = false;
          btn.textContent = "Start Benchmark";
          alert("Failed: " + (data.message || "Unknown error"));
        }
      }).catch(function(e) {
        btn.disabled = false;
        btn.textContent = "Start Benchmark";
        alert("Error: " + e);
      });
  });

  // --- Polling loop for progress output (supports multiple parallel runs) ---
  function startPolling(instId, runId) {
    let poll = setInterval(function() {
      fetch(API_BASE + "/benchmarks/results/" + runId + "/progress")
        .then(function(r) { return r.json(); })
        .then(function(data) {
          if (data.status !== "ok" || !data.data) return;
          let out = document.getElementById("output-window");
          let snap = data.data.output_snapshot || "";
          out.textContent = snap || "Waiting for output...\n";
          out.scrollTop = out.scrollHeight;

          if (!data.data.running) {
            clearInterval(poll);
            delete activeRuns[instId];
            let statusEl = document.getElementById("bench-status");
            let runningCount = Object.keys(activeRuns).length;
            statusEl.textContent = runningCount > 0
              ? runningCount + " benchmark(s) running"
              : "All benchmarks complete";
            loadResults();
          }
        });
    }, 3000);
  }

  // --- Auto-refresh for running benchmarks (every 5s) ---
  function startAutoRefresh() {
    if (autoRefreshInterval) clearInterval(autoRefreshInterval);
    autoRefreshInterval = setInterval(function() {
      let sortKey = qrSettings.get('benchmark', 'sort_col', '');
      let sortDir = qrSettings.get('benchmark', 'sort_dir', 'desc');
      loadResults(sortKey, sortDir);
    }, 5000);
  }

  // --- Populate instance filter dropdown from instances list ---
  let allInstances = window.__BENCH_CONFIG__.instances || [];

  // Instance lookup map — replaces linear searches (stores name + state)
  const _instMap = {};
  for (let _i = 0; _i < allInstances.length; _i++) {
    let _a = allInstances[_i];
    _instMap[_a.id] = {name: _a.name, state: _a.state || ''};
  }

  function populateInstanceFilter() {
    let sel = document.getElementById("bench-filter");
    while (sel.options.length > 1) sel.remove(1);
    allInstances.forEach(function(inst) {
      if (inst.state !== 'running') return;
      if (inst.engine_type_name !== 'llama_server' && inst.engine_type_name !== 'llama.cpp') return;
      let opt = document.createElement("option");
      opt.value = inst.id;
      opt.textContent = inst.name + ' (' + (inst.node_hostname || inst.node_name) + ')';
      sel.appendChild(opt);
    });
  }

  // --- When filter dropdown changes, also select the instance in main dropdown ---
  document.getElementById("bench-filter").addEventListener("change", function() {
    let filterVal = this.value;
    if (filterVal !== "all") {
      document.getElementById("instance-select").value = filterVal;
      document.getElementById("instance-select").dispatchEvent(new Event('change'));
    }
    loadResults();
  });

// --- Load results table (consolidated: running first, then completed/failed) ---
  function loadResults(sortKey, sortDir) {
    let filterVal = document.getElementById("bench-filter").value;
    let limitVal = document.getElementById("result-limit").value;
    let url = API_BASE + "/benchmarks/results?instance_id=" + encodeURIComponent(filterVal) + "&limit=" + encodeURIComponent(limitVal);
    fetch(url)
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.status !== "ok") return;
        let all = data.items || [];
        // Running: success is 0 or missing (incomplete); Done: success=1 or success=-1
        let running = all.filter(function(r){ return r.success === BENCH_RUNNING || r.success == null; });
        let done = all.filter(function(r){ return r.success !== BENCH_RUNNING && r.success != null; });
        renderResultsTable(running.concat(done), "results-body", sortKey, sortDir);
      });
  }

  // --- Render results table ---
  function renderResultsTable(items, tbodyId, sortKey, sortDir) {
    let tbody = document.getElementById(tbodyId);
    tbody.innerHTML = "";
    if (items.length === 0) {
      tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:#888;">No benchmarks run yet.</td></tr>';
      return;
    }

    // Running first (stable), then completed/failed sorted by started_at DESC
    let running = items.filter(function(r){ return r.success === BENCH_RUNNING || r.success == null; });
    let done = items.filter(function(r){ return r.success !== BENCH_RUNNING && r.success != null; });
    done.sort(function(a,b) { return (b.started_at || '').localeCompare(a.started_at || ''); });

    running.forEach(function(r) { renderRow(r, tbody, true); });
    done.forEach(function(r) { renderRow(r, tbody, false); });

    // Re-apply sort if we have saved sort state (auto-refresh or manual re-sort)
    if (sortKey && sortDir) {
      _applyTableSort(tbody, sortKey, sortDir);
    }
  }

  function renderRow(r, tbody, isRunning) {
    let tr = document.createElement("tr");
    if (isRunning) tr.style.background = '#f0f8f0';
    let statusClass = r.success === BENCH_SUCCESS ? 'badge-success' : (r.success === BENCH_FAILED ? 'badge-error' : 'badge-running');
    let statusText = isRunning ? 'running' : (r.success === BENCH_SUCCESS ? 'success' : 'failed');
    let dur = r.duration_ms ? (r.duration_ms / 1000).toFixed(1) + 's' : '-';
    let pps = '-', ops = '-';
    try {
      let ej = r.response_json;
      if (typeof ej === 'string') ej = JSON.parse(ej);
      let t = (ej && ej.timings) || {};
      if (t.prompt_per_second) pps = parseFloat(t.prompt_per_second).toFixed(2);
      if (t.predicted_per_second) ops = parseFloat(t.predicted_per_second).toFixed(2);
    } catch(e) {}

    // Look up instance name from map
    let instName = _instMap[r.instance_id]?.name || '?';

    let timeStr = (r.started_at || '-');
    tr.innerHTML = '<td>' + timeStr + '</td>' +
                   '<td class="bench-inst-name" style="cursor:pointer;color:#1565c0;" title="Click to select instance">' + instName + '</td>' +
                   '<td>' + (r.prompt_name || '-') + '</td>' +
                   '<td>' + (r.model_name || '-') + '</td>' +
                   '<td>' + (r.preset_name || '-') + '</td>' +
                   '<td>' + dur + '</td>' +
                   '<td>' + pps + '</td>' +
                   '<td>' + ops + '</td>' +
                   '<td><span class="badge ' + statusClass + '">' + statusText + '</span></td>' +
                   '<td><button class="btn btn-primary" style="font-size:0.8em;padding:2px 6px;" onclick="viewResult(\'' + r.run_id + '\')">View</button> <button class="btn btn-danger bench-del-btn" data-id="' + r.run_id + '" title="Delete this result" style="font-size:0.7em;padding:1px 4px;">&#10006;</button></td>';
    tbody.appendChild(tr);

    // Add click handler on instance name cell to select the instance (now col 1)
    let instCell = tr.cells[1];
    instCell.addEventListener('click', function() {
      document.getElementById("instance-select").value = r.instance_id;
      document.getElementById("instance-select").dispatchEvent(new Event('change'));
    });
    // Add click handler on delete button
    let delBtn = tr.querySelector('.bench-del-btn');
    if (delBtn) {
      delBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        let rid = this.getAttribute('data-id');
        if (!confirm('Delete benchmark result ' + rid.substring(0,12) + '?')) return;
        fetch(API_BASE + '/benchmarks/results/' + rid, { method: 'DELETE' })
          .then(function(r) { return r.json(); })
          .then(function(data) {
            if (data.status === 'ok') {
              let sortKey = qrSettings.get('benchmark', 'sort_col', '');
              let sortDir = qrSettings.get('benchmark', 'sort_dir', 'desc');
              loadResults(sortKey, sortDir);
            } else {
              alert('Delete failed: ' + (data.message || JSON.stringify(data)));
            }
          })
          .catch(function(e) { alert('Error: ' + e); });
      });
    }
  }

  // --- Result detail modal ---
  window.viewResult = function(runId) {
    fetch(API_BASE + "/benchmarks/results/" + runId)
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.status !== "ok" || !data.data) return;
        let d = data.data;

        let metricHtml = '';
        // Look up instance name from map
        let instName = _instMap[d.instance_id]?.name || '?';
        let metrics = [
          ['Run ID', d.run_id],
          ['Instance', instName],
          ['Time', d.started_at || '-'],
          ['Duration', d.duration_ms ? (d.duration_ms / 1000).toFixed(1) + 's' : '-'],
          ['Status', d.success === BENCH_SUCCESS ? 'Success' : 'Failed'],
          ['Prompt', d.prompt_name || '-'],
          ['Model', d.model_name || '-'],
          ['Preset', d.preset_name || '-'],
          ['Node', d.node_name || '-'],
        ];
        try {
          let ej = d.response_json;
          if (typeof ej === 'string') ej = JSON.parse(ej);
          let t = (ej && ej.timings) || {};
          if (t.prompt_per_second) metrics.push(['Prompt/s', parseFloat(t.prompt_per_second).toFixed(2)]);
          if (t.predicted_per_second) metrics.push(['Predicted/s', parseFloat(t.predicted_per_second).toFixed(2)]);
          if (t.prompt_ms) metrics.push(['Prompt time', t.prompt_ms + 'ms']);
          if (t.predicted_ms) metrics.push(['Generation time', t.predicted_ms + 'ms']);
          if (t.prompt_n) metrics.push(['Prompt tokens', t.prompt_n]);
          if (t.predicted_n) metrics.push(['Generated tokens', t.predicted_n]);
        } catch(e) {}

        if (d.finished_at) metrics.push(['Finished', d.finished_at]);

        metrics.forEach(function(m) {
          metricHtml += '<div class="detail-card"><label>' + m[0] + '</label><value>' + m[1] + '</value></div>';
        });
        document.getElementById("modal-metrics").innerHTML = metricHtml;

        document.getElementById("modal-output").textContent = d.output || '(no output)';

        let rawJson = '';
        try {
          let ej2 = d.response_json;
          if (typeof ej2 === 'string') {
            rawJson = JSON.stringify(JSON.parse(ej2), null, 2);
          } else {
            rawJson = JSON.stringify(ej2, null, 2);
          }
        } catch(e) {
          rawJson = String(d.response_json || 'N/A');
        }
        document.getElementById("modal-json").textContent = rawJson;

        document.getElementById("result-modal").style.display = "flex";
      });
  };

  // Close modal
  document.getElementById("modal-close").addEventListener("click", function() {
    document.getElementById("result-modal").style.display = "none";
  });
  document.getElementById("result-modal").addEventListener("click", function(e) {
    if (e.target === this) this.style.display = "none";
  });

  // --- Results table sorting (standard data-col pattern from sortable_tables.md) ---
  // Column index map by name — if columns reorder, update this mapping only
  let _SORT_COLS = {time:0, instance:1, prompt:2, model:3, preset:4, duration:5, pps:6, ops:7};
  // Numeric-sortable column indices
  let _NUM_COLS = {5: true, 6: true, 7: true};

  // Apply sort to existing table rows (used by auto-refresh to restore sort state)
  function _applyTableSort(tbody, sortKey, sortDir) {
    let col = _SORT_COLS[sortKey];
    if (col === undefined) return;
    let rows = Array.from(tbody.rows);
    if (rows.length <= 1) return;

    rows.sort(function(a, b) {
      let cellA = a.cells[col] ? a.cells[col].textContent.trim() : '';
      let cellB = b.cells[col] ? b.cells[col].textContent.trim() : '';
      if (col in _NUM_COLS) {
        let na = parseFloat(cellA.replace(/[^0-9.]/g, '')) || 0;
        let nb = parseFloat(cellB.replace(/[^0-9.]/g, '')) || 0;
        return sortDir === 'asc' ? na - nb : nb - na;
      }
      return sortDir === 'asc' ? cellA.localeCompare(cellB) : cellB.localeCompare(cellA);
    });

    rows.forEach(function(row) { tbody.appendChild(row); });
  }

  // Restore saved sort headers on page load
  (function() {
    let savedCol = qrSettings.get('benchmark', 'sort_col', '');
    let savedDir = qrSettings.get('benchmark', 'sort_dir', 'desc');
    if (!savedCol) return;

    document.querySelectorAll('#results-table .sortable').forEach(function(t) {
      t.textContent = t.textContent.replace(/\s*[\u2191\u2193]\s*$/, '').trim();
    });
    let targetTh = document.querySelector('#results-table .sortable[data-col="' + _SORT_COLS[savedCol] + '"]');
    if (targetTh) {
      let arrow = savedDir === 'asc' ? ' \u2191' : ' \u2193';
      targetTh.textContent = targetTh.textContent.trim() + arrow;
    }
  })();

  document.querySelectorAll('#results-table .sortable').forEach(function(th) {
    th.style.cursor = 'pointer';
    th.addEventListener('click', function() {
      let col = parseInt(this.getAttribute('data-col'), 10);
      let key = '';
      for (let k in _SORT_COLS) { if (_SORT_COLS[k] === col) { key = k; break; } }
      if (!key) return;

      let dir = qrSettings.get('benchmark', 'sort_dir', 'desc');
      dir = dir === 'desc' ? 'asc' : 'desc';
      qrSettings.set('benchmark', 'sort_col', key);
      qrSettings.set('benchmark', 'sort_dir', dir);

      // Update arrows on all sortable headers
      document.querySelectorAll('#results-table .sortable').forEach(function(t) {
        t.textContent = t.textContent.replace(/\s*[\u2191\u2193]\s*$/, '').trim();
      });
      let arrow = dir === 'asc' ? ' \u2191' : ' \u2193';
      this.textContent = this.textContent.trim() + arrow;

      // Sort tbody rows in place
      let tbody = document.getElementById('results-body');
      _applyTableSort(tbody, key, dir);
    });
  });

  // --- Result limit change handler ---
  document.getElementById("result-limit").addEventListener("change", function() {
    loadResults();
  });

  // --- Refresh button ---
  document.getElementById("refresh-results-btn").addEventListener("click", function() {
    let sortKey = qrSettings.get('benchmark', 'sort_col', '');
    let sortDir = qrSettings.get('benchmark', 'sort_dir', 'desc');
    loadResults(sortKey, sortDir);
  });

  // --- Clear results button ---
  document.getElementById("clear-results-btn").addEventListener("click", function() {
    if (!confirm('Clear all benchmark results? This cannot be undone.')) return;
    fetch(API_BASE + "/benchmarks/results", { method: "DELETE" })
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.status === "ok") {
          loadResults();
          alert("All results cleared.");
        } else {
          alert("Clear failed: " + (data.message || JSON.stringify(data)));
        }
      })
      .catch(function(e) { alert("Error: " + e); });
  });

   // --- Initial load ---
  populateInstanceFilter();
  setTimeout(function() { loadResults(); startAutoRefresh(); }, 500);

  // --- Periodic check for completed runs (updates running count display) ---
  setInterval(function() {
    let statusEl = document.getElementById("bench-status");
    if (!statusEl) return;
    let runningCount = Object.keys(activeRuns).length;
    if (runningCount > 0) {
      statusEl.textContent = runningCount + " benchmark(s) running";
    } else {
      statusEl.textContent = "";
    }
  }, 10000);

  // --- Periodic re-check of active runs via API (cleans up stale entries) ---
  setInterval(function() {
    let filterVal = document.getElementById("bench-filter").value;
    let url = API_BASE + "/benchmarks/results?instance_id=" + encodeURIComponent(filterVal) + "&limit=50";
    fetch(url)
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.status !== "ok") return;
        let all = data.items || [];
        // Sync activeRuns with API reality
        let apiRunningIds = {};
        all.forEach(function(r){
          if (r.success === BENCH_RUNNING || r.success == null) {
            apiRunningIds[r.instance_id] = r.run_id;
          }
        });
        // Remove stale entries from activeRuns
        for (let instId in activeRuns) {
          if (!apiRunningIds[instId]) {
            delete activeRuns[instId];
          }
        }
      });
  }, 15000);
})();
