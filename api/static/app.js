(function () {
  'use strict';

  function csrfCookieToken() {
    var prefix = 'csrf-token=';
    var match = document.cookie.split(';').map(function (part) { return part.trim(); })
      .find(function (part) { return part.startsWith(prefix); });
    return match ? decodeURIComponent(match.slice(prefix.length)) : '';
  }

  function csrfHeaders(headers) {
    var meta = document.querySelector('meta[name="csrf-token"]');
    var token = csrfCookieToken() || (meta && meta.content);
    if (token) headers['X-CSRF-Token'] = token;
    return headers;
  }

  document.body && document.body.addEventListener('htmx:configRequest', function (evt) {
    csrfHeaders(evt.detail.headers);
  });

  /* Chart.js global defaults */
  Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, Inter, sans-serif";
  Chart.defaults.font.size = 11;
  Chart.defaults.color = '#999992';
  Chart.defaults.borderColor = 'rgba(220, 220, 212, 0.14)';
  Chart.defaults.scale.grid.color = 'rgba(220, 220, 212, 0.08)';
  Chart.defaults.scale.ticks.color = '#6B6B66';
  Chart.defaults.elements.point.radius = 0;
  Chart.defaults.elements.line.borderWidth = 1.5;
  Chart.defaults.plugins.legend.display = false;
  Chart.defaults.animation = false;

  let modalChart = null;

  function chartRoots(root) {
    var nodes = [];
    if (root && root.matches && root.matches('[data-chart]')) nodes.push(root);
    if (root && root.querySelectorAll) {
      nodes = nodes.concat(Array.from(root.querySelectorAll('[data-chart]')));
    }
    return nodes;
  }

  function setChartBusy(container, busy) {
    if (container) container.setAttribute('aria-busy', busy ? 'true' : 'false');
  }

  function initCharts(root) {
    chartRoots(root || document).forEach(function (container) {
      if (container.dataset.chartInitialized === 'true') return;
      container.dataset.chartInitialized = 'true';
      setChartBusy(container, true);
      var canvas = container.querySelector('canvas');
      if (canvas) {
        var existing = Chart.getChart(canvas);
        if (existing) existing.destroy();
      }
    });
  }

  /* Modal chart ------------------------------------------------------------- */
  function setModalState(message, isError) {
    var status = document.getElementById('modal-status');
    if (!status) return;
    status.textContent = message || '';
    status.classList.toggle('error-block', Boolean(isError));
    status.hidden = !message;
  }

  function openModal(seriesId, label) {
    var modal = document.getElementById('indicator-modal');
    var title = document.getElementById('modal-title');
    title.textContent = label || seriesId;
    modal.classList.add('active');
    modal.setAttribute('aria-hidden', 'false');
    var chartContainer = document.getElementById('modal-chart-container');
    setChartBusy(chartContainer, true);
    setModalState('Loading series…', false);
    var modalCanvas = document.getElementById('modal-chart');
    var existing = modalCanvas ? Chart.getChart(modalCanvas) : null;
    if (existing) existing.destroy();
    if (modalChart) {
      modalChart.destroy();
      modalChart = null;
    }

    var fromDate = new Date();
    fromDate.setFullYear(fromDate.getFullYear() - 1);
    var fromStr = fromDate.toISOString().split('T')[0];

    fetch('/api/macro/' + encodeURIComponent(seriesId) + '?from=' + fromStr)
      .then(function (r) {
        if (!r.ok) throw new Error('Series unavailable');
        return r.json();
      })
      .then(function (data) {
        if (!data.observations || data.observations.length === 0) {
          setModalState('No observations found for this series.', true);
          return;
        }
        setModalState('', false);
        var ctx = document.getElementById('modal-chart').getContext('2d');
        modalChart = new Chart(ctx, {
          type: 'line',
          data: {
            labels: data.observations.map(function (o) { return o.observed_at; }),
            datasets: [{
              label: label || seriesId,
              data: data.observations.map(function (o) { return o.value; }),
              borderColor: '#DCDCD4',
              borderWidth: 1.5,
              pointRadius: 0,
              fill: false,
            }]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
              x: {
                display: true,
                grid: { color: 'rgba(220, 220, 212, 0.08)' },
                ticks: { color: '#6B6B66', maxTicksLimit: 12 }
              },
              y: {
                display: true,
                grid: { color: 'rgba(220, 220, 212, 0.08)' },
                ticks: { color: '#6B6B66' }
              }
            },
            plugins: {
              legend: { display: false }
            }
          }
        });
      })
      .catch(function (err) {
        console.error('Modal chart error for', seriesId, err);
        setModalState('Unable to load this series.', true);
      })
      .finally(function () {
        setChartBusy(chartContainer, false);
      });
  }

  function closeModal() {
    var modal = document.getElementById('indicator-modal');
    modal.classList.remove('active');
    modal.setAttribute('aria-hidden', 'true');
    if (modalChart) {
      modalChart.destroy();
      modalChart = null;
    }
  }

  function initModal() {
    var modal = document.getElementById('indicator-modal');
    var closeBtn = modal.querySelector('.modal-close');

    document.body.addEventListener('click', function (e) {
      var cell = e.target.closest('.indicator');
      if (cell) {
        e.preventDefault();
        openModal(cell.dataset.seriesId, cell.dataset.label);
      }
    });

    closeBtn.addEventListener('click', closeModal);
    modal.addEventListener('click', function (e) {
      if (e.target === modal) closeModal();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && modal.classList.contains('active')) {
        closeModal();
      }
    });
  }

  /* Card expand / collapse -------------------------------------------------- */
  function initCards() {
    document.body.addEventListener('click', function (e) {
      var card = e.target.closest('.instrument-card');
      if (!card) return;
      toggleInstrumentCard(card);
    });

    document.body.addEventListener('keydown', function (e) {
      var card = e.target.closest('.instrument-card');
      if (!card || (e.key !== 'Enter' && e.key !== ' ')) return;
      e.preventDefault();
      toggleInstrumentCard(card);
    });
  }

  function toggleInstrumentCard(card) {
    var symbol = card.dataset.symbol;
    if (!symbol) return;

    var alreadyExpanded = card.classList.contains('expanded');

    if (alreadyExpanded) {
      htmx.ajax('GET', '/partials/cards/clear', {target: '#expansion-panel', swap: 'outerHTML'});
    } else {
      htmx.ajax('GET', '/partials/cards/' + encodeURIComponent(symbol), {target: '#expansion-panel', swap: 'outerHTML'});
    }
  }

  function initIndicatorKeyboard() {
    document.body.addEventListener('keydown', function (e) {
      var indicator = e.target.closest('.indicator');
      if (!indicator || (e.key !== 'Enter' && e.key !== ' ')) return;
      e.preventDefault();
      openModal(indicator.dataset.seriesId, indicator.dataset.label);
    });
  }

  /* Expansion panel state sync ---------------------------------------------- */
  function initExpansionPanelSync() {
    document.body.addEventListener('htmx:afterSwap', function (evt) {
      if (evt.detail.target.id === 'expansion-panel') {
        var panel = document.getElementById('expansion-panel');
        var symbol = panel ? panel.dataset.symbol : null;
        document.querySelectorAll('.instrument-card').forEach(function (c) {
          c.classList.toggle('expanded', c.dataset.symbol === symbol);
        });
      }
    });
  }

  /* Progressive provenance and regime history ------------------------------ */
  function initProvenance() {
    document.body.addEventListener('toggle', function (e) {
      var details = e.target.closest('.provenance-details');
      if (!details || !details.open || details.dataset.loaded === 'true') return;
      details.dataset.loaded = 'true';

      var opinionId = details.dataset.opinionId;
      var evidenceTarget = details.querySelector('[data-evidence-target]');
      if (opinionId && evidenceTarget) {
        evidenceTarget.textContent = 'Loading evidence...';
        fetch(window.location.origin + '/api/evidence/' + encodeURIComponent(opinionId))
          .then(function (r) { if (!r.ok) throw new Error('Evidence unavailable'); return r.json(); })
          .then(function (evidence) {
            evidenceTarget.replaceChildren();
            var processing = evidence.processing || {};
            var meta = document.createElement('div');
            meta.className = 'evidence-meta';
            meta.textContent = [
              processing.processor || evidence.opinion.opinion_type,
              processing.model_used || evidence.opinion.model_used,
              processing.correlation_id ? 'run ' + processing.correlation_id.slice(0, 8) : null
            ].filter(Boolean).join(' · ');
            var lineage = document.createElement('div');
            lineage.className = 'lineage-breadcrumb';
            var parts = [];
            if (evidence.records && evidence.records.macro_series) parts.push('macro_series');
            if (evidence.records && evidence.records.econ_events) parts.push('econ_events');
            if (evidence.processing && evidence.processing.processor) parts.push(evidence.processing.processor);
            if (evidence.opinion && evidence.opinion.opinion_type) parts.push(evidence.opinion.opinion_type);
            if (parts.length) {
              lineage.textContent = parts.join(' \u2192 ');
              evidenceTarget.insertBefore(lineage, evidenceTarget.firstChild);
            }
            evidenceTarget.appendChild(meta);
            Object.keys(evidence.records || {}).forEach(function (source) {
              var group = document.createElement('div');
              group.className = 'evidence-group';
              var label = document.createElement('div');
              label.className = 'expansion-label';
              label.textContent = source.replace('_', ' ') + ' · ' + evidence.records[source].length + ' records';
              group.appendChild(label);
              evidence.records[source].slice(0, 8).forEach(function (record) {
                var item = document.createElement('div');
                item.className = 'evidence-record tabular';
                item.textContent = Object.values(record).filter(function (v) { return v !== null; }).slice(0, 4).join(' · ');
                group.appendChild(item);
              });
              evidenceTarget.appendChild(group);
            });
          })
          .catch(function (err) { evidenceTarget.textContent = err.message; });
      }

      var historyTarget = details.querySelector('[data-history-target]');
      if (historyTarget) {
        fetch(window.location.origin + '/api/regime/history?days=' + encodeURIComponent(details.dataset.historyDays || '90'))
          .then(function (r) { return r.json(); })
          .then(function (data) {
            historyTarget.replaceChildren();
            if (!data.regimes || !data.regimes.length) {
              var emptyState = document.createElement('div');
              emptyState.className = 'empty-state';
              emptyState.textContent = 'No history available';
              historyTarget.appendChild(emptyState);
            } else {
              (data.regimes || []).slice().reverse().forEach(function (regime) {
              var point = document.createElement('div');
              var createdAt = new Date(regime.created_at);
              var direction = (regime.direction || 'neutral').toLowerCase();
              point.className = 'timeline-point bias-' + direction;
              point.title = regime.created_at + ' · ' + regime.regime + ' · ' + (regime.summary || '');
              var timestamp = document.createElement('span');
              timestamp.className = 'timeline-time tabular';
              timestamp.textContent = isNaN(createdAt.getTime())
                ? regime.created_at
                : createdAt.toLocaleDateString('en-US', {month:'short', day:'numeric'});
              var regimeName = document.createElement('span');
              regimeName.className = 'timeline-regime';
              regimeName.textContent = regime.regime || 'unknown';
              var meta = document.createElement('span');
              meta.className = 'timeline-meta';
              meta.textContent = direction + ' · ' + (regime.confidence || 'unknown') + ' confidence';
              point.appendChild(timestamp);
              point.appendChild(regimeName);
              point.appendChild(meta);
              historyTarget.appendChild(point);
            });
            }
          });
      }

      var compareTarget = details.querySelector('[data-compare-target]');
      if (compareTarget && !compareTarget.dataset.loaded) {
        compareTarget.dataset.loaded = 'true';
        setChartBusy(compareTarget, true);
        var compareStatus = compareTarget.querySelector('.chart-status');
        if (compareStatus) compareStatus.textContent = 'Loading comparison…';
        var indicators = ['T10Y2Y', 'VIXCLS', 'DTWEXBGS', 'BAMLH0A0HYM2', 'DGS10', 'T5YIE'];
        var colors = ['#DCDCD4', '#4FA86E', '#C44545', '#C9A227', '#6B6B66', '#999992'];
        var days = details.dataset.historyDays || '90';
        var fetchPromises = indicators.map(function(id) {
          return fetch(window.location.origin + '/api/macro/' + encodeURIComponent(id) + '?days=' + days)
            .then(function(r) { return r.ok ? r.json() : null; })
            .catch(function() { return null; });
        });
        Promise.all(fetchPromises).then(function(results) {
          var comparisonLabels = Array.from(new Set(results.flatMap(function(data) {
            return data && data.observations
              ? data.observations.map(function(o) { return String(o.observed_at).slice(0, 10); })
              : [];
          }))).sort();
          var datasets = [];
          results.forEach(function(data, i) {
            if (!data || !data.observations || !data.observations.length) return;
            var baseValue = Number(data.observations[0].value);
            if (!Number.isFinite(baseValue) || baseValue === 0) return;
            var valuesByDate = {};
            var rawValuesByDate = {};
            data.observations.forEach(function(o) {
              var rawValue = Number(o.value);
              var dateKey = String(o.observed_at).slice(0, 10);
              valuesByDate[dateKey] = (rawValue / baseValue) * 100;
              rawValuesByDate[dateKey] = rawValue;
            });
            datasets.push({
              label: indicators[i],
              data: comparisonLabels.map(function(dateKey) { return valuesByDate[dateKey] ?? Number.NaN; }),
              rawValues: comparisonLabels.map(function(dateKey) { return rawValuesByDate[dateKey] ?? null; }),
              borderColor: colors[i],
              borderWidth: 1,
              pointRadius: 0,
              spanGaps: true,
              tension: 0.1,
              fill: false
            });
          });
          if (!datasets.length) {
            if (compareStatus) compareStatus.textContent = 'No comparison observations available.';
            return;
          }
          var canvas = compareTarget.querySelector('canvas');
          if (!canvas) throw new Error('Comparison chart canvas unavailable');
          var existing = Chart.getChart(canvas);
          if (existing) existing.destroy();
          var ctx = canvas.getContext('2d');
          new Chart(ctx, {
            type: 'line',
            data: { labels: comparisonLabels, datasets: datasets },
            options: {
              responsive: true,
              maintainAspectRatio: false,
              scales: {
                x: {
                  type: 'category',
                  grid: { color: 'rgba(220,220,212,0.06)' },
                  ticks: { color: '#6B6B66', font: { size: 10 } }
                },
                y: {
                  grid: { color: 'rgba(220,220,212,0.06)' },
                  title: { display: true, text: 'Indexed to 100', color: '#6B6B66' },
                  ticks: { color: '#6B6B66', font: { size: 10 } }
                }
              },
              plugins: {
                legend: {
                  display: true,
                  labels: { color: '#999992', font: { size: 11 }, usePointStyle: true }
                },
                tooltip: {
                  callbacks: {
                    label: function(context) {
                      var rawValue = context.dataset.rawValues[context.dataIndex];
                      if (rawValue === null || rawValue === undefined) return context.dataset.label;
                      return context.dataset.label + ': ' + rawValue + ' (' + context.parsed.y.toFixed(1) + ' indexed)';
                    }
                  }
                }
              }
            }
          });
          if (compareStatus) compareStatus.hidden = true;
        }).catch(function (error) {
          console.error('Regime comparison chart error', error);
          if (compareStatus) {
            compareStatus.hidden = false;
            compareStatus.textContent = 'Unable to load macro comparison.';
          }
        }).finally(function () {
          setChartBusy(compareTarget, false);
        });
      }
    }, true);
  }

  function initLiveQuotes() {
    if (!window.EventSource || !document.querySelector('[data-live-price]')) return;
    function connect(hasRefreshed) {
      fetch('/api/quotes/stream-token', {credentials: 'same-origin'})
        .then(function (response) { if (!response.ok) throw new Error('stream token unavailable'); })
        .then(function () {
          var source = new EventSource('/api/quotes/stream');
          source.onmessage = function (event) {
            var payload = JSON.parse(event.data);
            (payload.quotes || []).forEach(function (quote) {
              document.querySelectorAll('[data-live-price="' + quote.symbol + '"]').forEach(function (el) {
                el.textContent = Number(quote.price).toPrecision(6);
              });
              document.querySelectorAll('[data-live-time="' + quote.symbol + '"]').forEach(function (el) {
                el.textContent = 'live · ' + new Date(quote.observed_at).toISOString().slice(11, 19) + ' UTC';
              });
            });
          };
          source.onerror = function () {
            source.close();
            if (!hasRefreshed) connect(true);
          };
        });
    }
    connect(false);
  }

  /* Log row expand / collapse ----------------------------------------------- */
  function initLogs() {
    var expandedLogIds = new Set();

    document.body.addEventListener('click', function (e) {
      var row = e.target.closest('#logs-table-body tr[data-log-id]');
      if (row) {
        var id = row.getAttribute('data-log-id');
        var detailRow = document.querySelector('#logs-table-body tr[data-detail-for="' + id + '"]');
        if (detailRow) {
          detailRow.classList.toggle('expanded');
          if (detailRow.classList.contains('expanded')) expandedLogIds.add(id);
          else expandedLogIds.delete(id);
        }
      }
    });

    document.body.addEventListener('htmx:afterSwap', function (evt) {
      if (!evt.detail.target || evt.detail.target.id !== 'logs-table-body') return;
      expandedLogIds.forEach(function (id) {
        var detail = document.querySelector('#logs-table-body tr[data-detail-for="' + id + '"]');
        if (detail) detail.classList.add('expanded');
      });
    });

    document.body.addEventListener('click', function (e) {
      var chip = e.target.closest('.run-chip');
      if (!chip) return;
      var runId = chip.dataset.runId;
      var detail = document.getElementById('run-inspector-detail');
      var correlationInput = document.getElementById('logs-correlation-id');
      var isSelected = chip.classList.contains('selected');
      document.querySelectorAll('.run-chip').forEach(function (item) {
        item.classList.toggle('selected', !isSelected && item === chip);
      });
      if (isSelected) {
        if (correlationInput) {
          correlationInput.value = '';
          htmx.trigger(document.getElementById('logs-filter-form'), 'change');
        }
        if (detail) {
          detail.replaceChildren();
          detail.hidden = true;
        }
        return;
      }
      if (correlationInput) {
        correlationInput.value = runId;
        htmx.trigger(document.getElementById('logs-filter-form'), 'change');
      }
      fetch('/api/system/runs/' + encodeURIComponent(runId))
        .then(function (r) { if (!r.ok) throw new Error('Run unavailable'); return r.json(); })
        .then(function (run) {
          detail.replaceChildren();
          detail.hidden = false;
          var heading = document.createElement('div');
          heading.className = 'run-detail-heading';
          heading.textContent = (run.run_kind || 'run') + ' · ' + (run.result_status || run.status);
          detail.appendChild(heading);
          (run.stages || []).forEach(function (stage) {
            var row = document.createElement('button');
            row.type = 'button';
            row.className = 'run-stage';
            row.dataset.logId = stage.log_id;
            row.textContent = stage.component + ' · ' + stage.status + ' · ' + (stage.duration_ms || 0) + ' ms';
            row.addEventListener('click', function () {
              var logRow = document.querySelector('[data-log-id="' + stage.log_id + '"]');
              if (logRow) {
                logRow.scrollIntoView({behavior: 'smooth', block: 'center'});
                logRow.classList.add('highlight');
                setTimeout(function () { logRow.classList.remove('highlight'); }, 1600);
              }
            });
            detail.appendChild(row);
          });
        })
        .catch(function (err) {
          detail.hidden = false;
          detail.textContent = err.message;
        });
    });
  }

  /* Cycle controls (Settings page) ------------------------------------------ */
  function initCycleButton() {
    var runBtn = document.getElementById('run-cycle-btn');
    var forceBtn = document.getElementById('force-cycle-btn');
    if (!runBtn) return;

    var progressEl = document.getElementById('cycle-progress');
    var progressText = document.getElementById('cycle-progress-text');
    var progressFill = document.getElementById('cycle-progress-fill');
    var resultEl = document.getElementById('cycle-result');

    function dispatchCycleRefresh() {
      document.body.dispatchEvent(new CustomEvent('cycleComplete', { bubbles: true }));
    }

    function setButtons(disabled) {
      runBtn.disabled = disabled;
      if (forceBtn) forceBtn.disabled = disabled;
    }

    function showProgress(text, pct) {
      if (progressEl) progressEl.hidden = false;
      if (progressText) progressText.textContent = text;
      if (progressFill) progressFill.style.width = (pct || 0) + '%';
      if (resultEl) resultEl.hidden = true;
    }

    function showResult(text, isError) {
      if (progressEl) progressEl.hidden = true;
      if (resultEl) {
        resultEl.hidden = false;
        resultEl.textContent = text;
        resultEl.style.color = isError ? 'var(--bear)' : 'var(--fg-muted)';
      }
    }

    function triggerCycle(mode) {
      if (runBtn.disabled) return;
      setButtons(true);
      showProgress('Starting ' + mode.replace('_', ' ') + '…', 5);

      fetch('/api/triggers/cycle', {
        method: 'POST',
        headers: csrfHeaders({'Content-Type': 'application/json'}),
        body: JSON.stringify({mode: mode, budget_confirmed: mode === 'force_full'})
      }).then(function (response) {
        if (response.status === 202) return response.json();
        var labels = {
          409: 'Cycle already running',
          422: 'Cycle request rejected',
          503: 'Cycle unavailable'
        };
        var error = new Error(labels[response.status] || 'Cycle failed — try again');
        error.status = response.status;
        throw error;
      }).then(function (response) {
        htmx.ajax('GET', '/partials/cards/clear', {target: '#expansion-panel', swap: 'outerHTML'});
        pollCycleCompletion(response.job_id);
      }).catch(function (error) {
        showResult(error.message, true);
        setButtons(false);
        dispatchCycleRefresh();
      });
    }

    function pollCycleCompletion(correlationId) {
      var maxAttempts = 100;
      var attempts = 0;

      function poll() {
        attempts++;
        if (attempts > maxAttempts) {
          showResult('Cycle taking longer than expected — check logs', true);
          setButtons(false);
          dispatchCycleRefresh();
          return;
        }
        fetch('/api/system/cycle-status?correlation_id=' + encodeURIComponent(correlationId))
          .then(function (r) {
            if (!r.ok) throw new Error('Cycle status unavailable');
            return r.json();
          })
          .then(function (data) {
            var progress = data.progress || {};
            var total = progress.total_stages || 1;
            var completed = progress.completed_stages !== undefined
              ? progress.completed_stages
              : (data.stages || []).filter(function (s) { return !['pending', 'running'].includes(s.status); }).length;
            var pct = Math.min(95, Math.round((completed / total) * 100));

            var successfulTerminal = ['success', 'partial', 'completed'];
            var failedTerminal = [
              'failed',
              'validation_failed',
              'budget_denied',
              'budget_blocked',
              'budget_unavailable'
            ];
            if (successfulTerminal.includes(data.status)) {
              showProgress('Cycle finished: ' + data.status, 100);
              setTimeout(function () { showResult('Cycle completed: ' + data.status, false); }, 600);
              setButtons(false);
              dispatchCycleRefresh();
            } else if (failedTerminal.includes(data.status)) {
              showResult('Cycle ' + data.status.replaceAll('_', ' ') + ' — check logs', true);
              setButtons(false);
              dispatchCycleRefresh();
            } else {
              var current = progress.current_stage;
              var label = current
                ? 'Running ' + current.replaceAll('_', ' ') + '…'
                : 'Cycle running…';
              showProgress(label + ' (' + completed + '/' + total + ')', pct);
              setTimeout(poll, 2000);
            }
          })
          .catch(function () {
            setTimeout(poll, 3000);
          });
      }

      poll();
    }

    document.body.addEventListener('click', function (event) {
      var btn = event.target.closest('#run-cycle-btn, #force-cycle-btn');
      if (!btn) return;
      var mode = btn.getAttribute('data-mode') || 'refresh';
      if (mode === 'force_full') {
        if (!window.confirm('Force full re-runs every collector and processor. Uses more budget. Continue?')) return;
      }
      triggerCycle(mode);
    });
  }

  /* Data chip (header) ------------------------------------------------------ */
  function initDataChip() {
    document.addEventListener('click', function (event) {
      var chip = document.getElementById('data-chip');
      if (!chip || !chip.open) return;
      if (event.target.closest('#data-chip')) return;
      chip.open = false;
    });
  }

  function initTimezoneControl(root) {
    var select = (root && root.matches && root.matches('#display-timezone'))
      ? root
      : (root && root.querySelector ? root.querySelector('#display-timezone') : null);
    if (!select || select.dataset.bound === 'true') return;
    select.dataset.bound = 'true';
    var status = document.getElementById('timezone-status');
    var previous = select.value;

    fetch('/api/settings/timezone', {method: 'GET'})
      .then(function (response) {
        if (!response.ok) throw new Error('Timezone unavailable');
        return response.json();
      })
      .then(function (setting) {
        select.replaceChildren();
        (setting.choices || []).forEach(function (choice) {
          var option = document.createElement('option');
          option.value = choice;
          option.textContent = choice;
          option.selected = choice === setting.current;
          select.appendChild(option);
        });
        previous = setting.current;
      })
      .catch(function () {
        select.value = previous;
        if (status) status.textContent = 'Timezone unavailable';
      });

    select.addEventListener('change', function () {
      var requested = select.value;
      select.disabled = true;
      if (status) status.textContent = 'Saving…';
      fetch('/api/settings/timezone', {
        method: 'POST',
        headers: csrfHeaders({'Content-Type': 'application/json'}),
        body: JSON.stringify({timezone: requested})
      }).then(function (response) {
        if (!response.ok) throw new Error('Timezone not saved');
        return response.json();
      }).then(function () {
        window.location.reload();
      }).catch(function () {
        select.value = previous;
        select.disabled = false;
        if (status) status.textContent = 'Timezone not saved';
      });
    });
  }

  /* Unified refresh heartbeat and server-sent invalidations ------------------ */
  var MARKET_REFRESH_INTERVAL_MS = 90000;
  var marketRefreshTimer = null;
  var refreshBound = false;
  var liveStream = null;
  var liveStreamHealthy = false;
  var liveEventHandlers = Object.create(null);
  var liveRefreshes = Object.create(null);

  function registeredLiveSections(eventName, sectionKey) {
    return Array.prototype.filter.call(
      document.querySelectorAll('[data-live-section][data-live-event][data-live-url]'),
      function (section) {
        return (!eventName || section.dataset.liveEvent === eventName)
          && (!sectionKey || section.dataset.liveSection === sectionKey);
      }
    );
  }

  function refreshLiveSection(section) {
    if (!window.htmx || !section || !section.isConnected) return;
    var sectionKey = section.dataset.liveSection;
    var url = section.dataset.liveUrl;
    if (!sectionKey || !url || liveRefreshes[sectionKey]) return;

    liveRefreshes[sectionKey] = true;
    var request;
    try {
      request = window.htmx.ajax('GET', url, {
        target: section,
        swap: 'outerHTML'
      });
    } catch (_error) {
      delete liveRefreshes[sectionKey];
      return;
    }
    Promise.resolve(request).then(function () {
      delete liveRefreshes[sectionKey];
    }, function () {
      delete liveRefreshes[sectionKey];
    });
  }

  function refreshLiveSections(eventName, sectionKey) {
    registeredLiveSections(eventName, sectionKey).forEach(refreshLiveSection);
  }

  function dispatchMarketRefresh() {
    document.body.dispatchEvent(new CustomEvent('marketRefresh', { bubbles: true }));
  }

  function handleVisibilityChange() {
    if (document.hidden) {
      if (marketRefreshTimer) {
        window.clearInterval(marketRefreshTimer);
        marketRefreshTimer = null;
      }
      return;
    }
    dispatchMarketRefresh();
    ensureMarketRefresh();
  }

  function ensureMarketRefresh() {
    if (marketRefreshTimer) return;
    if (!refreshBound) {
      refreshBound = true;
      document.addEventListener('visibilitychange', handleVisibilityChange);
      document.body.addEventListener('marketRefresh', refreshLiveSectionsOnHeartbeat);
    }
    marketRefreshTimer = window.setInterval(dispatchMarketRefresh, MARKET_REFRESH_INTERVAL_MS);
  }

  function refreshLiveSectionsOnHeartbeat() {
    if (liveStreamHealthy || !registeredLiveSections().length) return;
    refreshLiveSections();
  }

  function registerLiveEvent(eventName, refreshAll) {
    if (!liveStream || liveEventHandlers[eventName]) return;
    liveEventHandlers[eventName] = function (event) {
      var sectionKey = null;
      if (!refreshAll && event && event.data) {
        try {
          var payload = JSON.parse(event.data);
          if (payload && typeof payload.section_key === 'string') {
            sectionKey = payload.section_key;
          }
        } catch (_error) {
          sectionKey = null;
        }
      }
      refreshLiveSections(refreshAll ? null : eventName, sectionKey);
    };
    liveStream.addEventListener(eventName, liveEventHandlers[eventName]);
  }

  function initLiveSections() {
    ensureMarketRefresh();
    var sections = registeredLiveSections();
    if (!sections.length) return;
    if (!window.EventSource) return;

    if (!liveStream) {
      try {
        liveStream = new window.EventSource('/stream');
        liveStream.onopen = function () { liveStreamHealthy = true; };
        liveStream.onerror = function () { liveStreamHealthy = false; };
      } catch (_error) {
        liveStream = null;
        return;
      }
    }
    registerLiveEvent('resync_required', true);

    sections.forEach(function (section) {
      registerLiveEvent(section.dataset.liveEvent);
    });
  }

  function initDynamicUi(root) {
    initCharts(root);
    initTimezoneControl(root);
    initLiveSections();
  }

  /* Boot --------------------------------------------------------------------- */
  function initSinceLastView() {
    var marker = document.querySelector('[data-since-last-view-marker]');
    if (!marker) return;
    fetch('/api/dashboard/last-view', {
      method: 'POST',
      headers: csrfHeaders({'Content-Type': 'application/json'})
    }).catch(function () {});
  }

  document.addEventListener('DOMContentLoaded', function () {
    ensureMarketRefresh();
    initCharts(document);
    initModal();
    initCards();
    initIndicatorKeyboard();
    initExpansionPanelSync();
    initProvenance();
    initLiveQuotes();
    initLogs();
    initCycleButton();
    initDataChip();
    initTimezoneControl(document);
    initLiveSections();
    initSinceLastView();
  });

  ['htmx:afterSwap', 'htmx:afterSettle'].forEach(function (eventName) {
    document.body.addEventListener(eventName, function (evt) {
      if (evt.detail && evt.detail.target) initCharts(evt.detail.target);
      if (evt.detail && evt.detail.target) initDynamicUi(evt.detail.target);
    });
  });
})();
