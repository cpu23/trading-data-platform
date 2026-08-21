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
    if (!refreshBound) {
      refreshBound = true;
      document.addEventListener('visibilitychange', handleVisibilityChange);
      document.body.addEventListener('marketRefresh', refreshLiveSectionsOnHeartbeat);
    }
    if (marketRefreshTimer || document.hidden) return;
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
    initThesisViews(root);
  }

  /* Bounded thesis tournament views ----------------------------------------- */
  var thesisUnknown = '\u2014';
  var thesisDefaultMinimumScore = '0.25';
  var thesisEligibilityBlockerLabels = {
    status: 'status',
    score: 'score',
    scenarios: 'scenarios',
    risks: 'risks',
    evidence: 'evidence',
    falsification: 'falsification',
    actionability: 'actionability',
    opposition: 'opposition'
  };

  function thesisValue(value) {
    if (value === null || value === undefined || value === '') return thesisUnknown;
    return String(value);
  }

  function thesisNumber(value, style) {
    if (value === null || value === undefined || value === '' || !Number.isFinite(Number(value))) {
      return thesisUnknown;
    }
    var number = Number(value);
    if (style === 'return') {
      return (number > 0 ? '+' : '') + (number * 100).toFixed(1) + '%';
    }
    if (style === 'count') return String(Math.trunc(number));
    return number.toFixed(2);
  }

  function thesisDate(value) {
    if (!value) return thesisUnknown;
    var parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return thesisUnknown;
    return parsed.toLocaleString([], {
      year: 'numeric', month: 'short', day: '2-digit',
      hour: '2-digit', minute: '2-digit'
    });
  }

  function thesisElement(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function thesisAppendText(parent, tag, className, text) {
    var child = thesisElement(tag, className, text);
    parent.appendChild(child);
    return child;
  }

  function thesisArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function thesisFirstDefined() {
    for (var index = 0; index < arguments.length; index += 1) {
      if (arguments[index] !== null && arguments[index] !== undefined && arguments[index] !== '') {
        return arguments[index];
      }
    }
    return null;
  }

  function thesisObject(value) {
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  }

  function thesisFetch(url, options, signal) {
    var requestOptions = options || {};
    requestOptions.credentials = 'same-origin';
    requestOptions.signal = signal;
    return fetch(url, requestOptions).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok) {
          var error = new Error('Request unavailable');
          error.status = response.status;
          error.body = body;
          throw error;
        }
        return body;
      });
    });
  }

  function thesisSetBusy(root, busy) {
    root.querySelectorAll('[aria-busy]').forEach(function (node) {
      node.setAttribute('aria-busy', busy ? 'true' : 'false');
    });
    root.querySelectorAll('[data-thesis-refresh], [data-thesis-run]').forEach(function (button) {
      button.disabled = busy;
      button.setAttribute('aria-busy', busy ? 'true' : 'false');
    });
  }

  function thesisMessage(root, text, kind) {
    var node = root.querySelector('[data-thesis-message]');
    if (!node) return;
    node.textContent = text;
    node.dataset.state = kind || 'ready';
  }

  function thesisStatusValue(root, key, value) {
    var node = root.querySelector('[data-status-value="' + key + '"]');
    if (node) node.textContent = thesisValue(value);
  }

  function thesisJobCounts(jobs) {
    if (!Array.isArray(jobs)) return thesisUnknown;
    var active = 0;
    jobs.forEach(function (job) {
      var state = String(job.state || '').toLowerCase();
      if (['queued', 'leased', 'running', 'failed_retryable'].indexOf(state) >= 0) active += 1;
    });
    return active + ' active / ' + jobs.length + ' recent';
  }

  function thesisLatestCycleAt(status) {
    status = thesisObject(status);
    var latestValue = null;
    var latestTime = -Infinity;
    if (Array.isArray(status.autonomy_jobs)) {
      status.autonomy_jobs.forEach(function (job) {
        var value = thesisObject(job).completed_at;
        var parsed = Date.parse(value || '');
        if (Number.isFinite(parsed) && parsed > latestTime) {
          latestValue = value;
          latestTime = parsed;
        }
      });
    }
    return latestValue || status.latest_evaluation_at;
  }

  function thesisModelCost(status) {
    var modelCost = thesisObject(thesisObject(status).model_cost);
    var cost = 'cost unavailable';
    if (
      modelCost.today_usd !== null &&
      modelCost.today_usd !== undefined &&
      Number.isFinite(Number(modelCost.today_usd))
    ) {
      cost = '$' + Number(modelCost.today_usd).toFixed(2);
    }
    var text = cost + ' · ' + thesisNumber(modelCost.attempts, 'count') + ' calls';
    var unknown = Number(modelCost.unknown_cost_attempts);
    if (Number.isFinite(unknown) && unknown > 0) {
      text += ' · ' + Math.trunc(unknown) + ' unknown';
    }
    return text;
  }

  function thesisCalibration(status) {
    var calibration = thesisObject(thesisObject(status).calibration);
    var count = Number(calibration.resolved_with_probability);
    var brier = Number(calibration.brier_score);
    if (!Number.isFinite(count) || count <= 0 || !Number.isFinite(brier)) {
      return 'Awaiting resolved forecasts';
    }
    return 'Brier ' + brier.toFixed(3) + ' · n=' + Math.trunc(count);
  }

  function thesisSourcesPartial(status) {
    var sources = thesisObject(thesisObject(status).sources);
    var names = Object.keys(sources);
    if (!names.length) return false;
    return names.some(function (name) {
      var collectionStatus = String(
        thesisObject(thesisObject(sources[name]).collection).status || ''
      );
      return collectionStatus !== 'success';
    });
  }

  function renderThesisStatus(root, status, groups, opportunities) {
    status = thesisObject(status);
    if (status.available === false) {
      ['last-cycle', 'model-cost', 'jobs', 'evidence', 'groups', 'positions', 'falsification', 'calibration', 'directions', 'top-opportunity'].forEach(function (key) {
        thesisStatusValue(root, key, thesisUnknown);
      });
      return;
    }
    var groupStatus = thesisObject(thesisObject(status.groups).by_status);
    var evidence = thesisObject(status.evidence);
    var evidenceCount = evidence.total;
    var sources = thesisObject(status.sources);
    var sourceNames = Object.keys(sources);
    var sourceDisplay = thesisUnknown;
    if (sourceNames.length) {
      var availableSources = sourceNames.filter(function (name) {
        var source = thesisObject(sources[name]);
        return thesisObject(source.data).available === true ||
          ['success', 'partial'].indexOf(String(thesisObject(source.collection).status)) >= 0;
      }).length;
      sourceDisplay = availableSources + '/' + sourceNames.length;
    } else if (status.source_count !== null && status.source_count !== undefined) {
      sourceDisplay = thesisNumber(status.source_count, 'count');
    }
    thesisStatusValue(root, 'last-cycle', thesisDate(thesisLatestCycleAt(status)));
    thesisStatusValue(root, 'jobs', thesisJobCounts(status.autonomy_jobs));
    thesisStatusValue(root, 'model-cost', thesisModelCost(status));
    thesisStatusValue(
      root,
      'evidence',
      sourceDisplay + ' sources / ' + thesisNumber(evidenceCount, 'count') + ' evidence'
    );
    var activeGroups = groupStatus.active;
    if (activeGroups === undefined && Array.isArray(groups)) {
      activeGroups = groups.filter(function (group) {
        return String(group.status || '').toLowerCase() === 'active';
      }).length;
    }
    thesisStatusValue(root, 'groups', thesisNumber(activeGroups, 'count'));
    thesisStatusValue(root, 'positions', thesisNumber(status.linked_theses, 'count'));
    thesisStatusValue(
      root,
      'falsification',
      status.latest_falsification_at ? 'last ' + thesisDate(status.latest_falsification_at) : thesisUnknown
    );
    thesisStatusValue(root, 'calibration', thesisCalibration(status));
    if (root.dataset.thesisView === 'research-preview') {
      var bull = null;
      var bear = null;
      var directionsComplete = Array.isArray(groups);
      if (directionsComplete) {
        bull = 0;
        bear = 0;
        groups.forEach(function (group) {
          if (group.long_count === null || group.long_count === undefined ||
              group.short_count === null || group.short_count === undefined) {
            directionsComplete = false;
            return;
          }
          bull += Number(group.long_count);
          bear += Number(group.short_count);
        });
      }
      if (!directionsComplete) {
        bull = null;
        bear = null;
      }
      thesisStatusValue(root, 'directions', bull === null ? thesisUnknown : bull + ' bull / ' + bear + ' bear');
      var top = thesisArray(opportunities)[0];
      thesisStatusValue(
        root,
        'top-opportunity',
        top ? thesisValue(top.claim || top.company || top.symbol) + ' · ' + thesisNumber(top.opportunity_score) : thesisUnknown
      );
    }
  }

  function thesisStateRow(tbody, columns, text) {
    tbody.textContent = '';
    var row = thesisElement('tr');
    var cell = thesisElement('td', 'thesis-state-cell', text);
    cell.colSpan = columns;
    row.appendChild(cell);
    tbody.appendChild(row);
  }

  function thesisCollectionState(root, selector, columns, text) {
    var target = root.querySelector(selector);
    if (!target) return;
    if (target.tagName === 'TBODY') {
      thesisStateRow(target, columns, text);
      return;
    }
    target.textContent = '';
    target.appendChild(thesisElement('li', 'thesis-state-cell', text));
  }

  function thesisMarkDetailUnavailable(root, text) {
    thesisField(root, 'claim', text);
    thesisField(root, 'group', 'No values inferred');
    thesisField(root, 'status', 'unavailable');
    root.querySelectorAll('.thesis-state-cell').forEach(function (node) {
      node.textContent = text;
    });
    var invalidations = root.querySelector('[data-thesis-invalidations]');
    if (invalidations) {
      invalidations.textContent = '';
      invalidations.appendChild(thesisElement('li', 'thesis-state-cell', text));
    }
  }

  function thesisCell(row, label, className, text) {
    var cell = thesisElement('td', className, text);
    cell.dataset.label = label;
    row.appendChild(cell);
    return cell;
  }

  function thesisOpportunityEmptyMessage(minimumScore) {
    return String(minimumScore) === '0'
      ? 'No eligible or ineligible opportunities meet these filters.'
      : 'No eligible opportunities meet these filters. Choose Any score to inspect ineligible theses.';
  }

  function thesisIneligibilityLabel(item) {
    if (item.eligible !== false) return '';
    var labels = [];
    var seen = {};
    var blockers = thesisArray(item.blockers);
    for (var index = 0; index < blockers.length && labels.length < 8; index += 1) {
      var blocker = String(blockers[index] || '').trim().toLowerCase();
      if (!Object.prototype.hasOwnProperty.call(thesisEligibilityBlockerLabels, blocker) || seen[blocker]) continue;
      seen[blocker] = true;
      labels.push(thesisEligibilityBlockerLabels[blocker]);
    }
    return 'Not eligible' + (labels.length ? ' · ' + labels.join(', ') : '');
  }

  function renderThesisOpportunities(root, opportunities, minimumScore) {
    var tbody = root.querySelector('[data-thesis-opportunities]');
    if (!tbody) return;
    tbody.textContent = '';
    if (!opportunities.length) {
      thesisStateRow(tbody, 8, thesisOpportunityEmptyMessage(minimumScore));
      return;
    }
    opportunities.forEach(function (item, index) {
      var row = thesisElement('tr');
      var ineligibilityLabel = thesisIneligibilityLabel(item);
      if (ineligibilityLabel) {
        row.classList.add('thesis-opportunity-ineligible');
        row.dataset.eligible = 'false';
      }
      var identity = thesisCell(row, 'Thesis / why now', 'thesis-opportunity-identity');
      var heading = thesisElement('div', 'thesis-opportunity-heading');
      thesisAppendText(heading, 'span', 'thesis-rank tabular', String(index + 1).padStart(2, '0'));
      var link = thesisElement('a', 'thesis-opportunity-link', thesisValue(item.claim || item.company || item.symbol));
      link.href = '/research/theses/' + encodeURIComponent(String(item.id || ''));
      heading.appendChild(link);
      identity.appendChild(heading);
      thesisAppendText(
        identity, 'span', 'thesis-opportunity-context',
        thesisValue(item.group_name) + ' · ' + thesisValue(item.mechanism)
      );
      thesisAppendText(
        identity, 'span', 'thesis-opportunity-catalyst',
        'Variant: ' + thesisValue(item.direction) + ' / ' + thesisValue(item.horizon) +
        ' · catalyst strength ' + thesisNumber(item.catalyst_score)
      );
      thesisCell(
        row, 'Direction', 'thesis-direction',
        thesisValue(item.direction) + ' · ' + thesisValue(item.horizon) + ' · ' + thesisValue(item.status)
      );
      var scoreCell = thesisCell(row, 'Score', 'thesis-primary-score tabular', thesisNumber(item.opportunity_score));
      if (ineligibilityLabel) {
        var eligibilityNote = thesisElement(
          'strong',
          'thesis-opportunity-catalyst thesis-ineligible-label',
          ineligibilityLabel
        );
        eligibilityNote.setAttribute('role', 'note');
        eligibilityNote.setAttribute('aria-label', 'Ranking eligibility: ' + ineligibilityLabel);
        scoreCell.appendChild(eligibilityNote);
      }
      thesisCell(
        row, 'EV / cost / downside', 'tabular',
        thesisNumber(item.expected_value, 'return') + ' / ' +
        thesisNumber(thesisFirstDefined(item.research_cost, item.expected_cost, item.cost)) + ' / ' +
        thesisNumber(item.expected_shortfall, 'return')
      );
      thesisCell(row, 'Confidence', 'tabular', thesisNumber(item.confidence_score));
      thesisCell(
        row, 'Evidence / contradiction', 'tabular',
        thesisNumber(item.evidence_strength) + ' / ' + thesisNumber(item.contradiction_strength)
      );
      thesisCell(
        row, 'Catalyst / neglect', 'tabular',
        thesisNumber(item.catalyst_score) + ' / ' + thesisNumber(item.neglect_score)
      );
      thesisCell(row, 'Evaluated', 'thesis-date tabular', thesisDate(item.last_evaluated_at));
      tbody.appendChild(row);
    });
  }

  function populateThesisGroupFilter(root, groups) {
    var select = root.querySelector('[data-thesis-group-filter]');
    if (!select) return;
    var selected = select.value;
    select.textContent = '';
    var all = thesisElement('option', '', 'All groups');
    all.value = '';
    select.appendChild(all);
    groups.forEach(function (group) {
      var option = thesisElement('option', '', thesisValue(group.name));
      option.value = thesisValue(group.id);
      select.appendChild(option);
    });
    if (selected && groups.some(function (group) { return String(group.id) === selected; })) {
      select.value = selected;
    }
  }

  function renderThesisGroups(root, groups) {
    var target = root.querySelector('[data-thesis-groups]');
    if (!target) return;
    target.textContent = '';
    if (!groups.length) {
      if (target.tagName === 'TBODY') thesisStateRow(target, 6, 'No tournament groups are available.');
      else target.appendChild(thesisElement('li', 'thesis-state-cell', 'No active tournament groups are available.'));
      return;
    }
    if (target.tagName !== 'TBODY') {
      groups.slice(0, 4).forEach(function (group) {
        var item = thesisElement('li', 'thesis-group-preview-row');
        var heading = thesisElement('div', 'thesis-group-preview-heading');
        thesisAppendText(heading, 'strong', '', thesisValue(group.name));
        thesisAppendText(heading, 'span', 'inv-status-pill', thesisValue(group.status));
        item.appendChild(heading);
        thesisAppendText(item, 'p', '', thesisValue(group.description));
        thesisAppendText(
          item, 'span', 'thesis-group-counts tabular',
          thesisNumber(group.long_count, 'count') + ' bull · ' +
          thesisNumber(group.short_count, 'count') + ' bear · ' +
          thesisNumber(group.neutral_count, 'count') + ' neutral · top ' +
          thesisNumber(group.max_opportunity) + ' · contradiction ' +
          thesisNumber(group.max_contradiction)
        );
        target.appendChild(item);
      });
      return;
    }
    groups.forEach(function (group) {
      var row = thesisElement('tr');
      var nameCell = thesisCell(row, 'Tournament', '');
      thesisAppendText(nameCell, 'strong', 'thesis-group-name', thesisValue(group.name));
      thesisAppendText(nameCell, 'span', 'thesis-group-description', thesisValue(group.description));
      var filterButton = thesisElement('button', 'thesis-filter-link', 'Show opportunities');
      filterButton.type = 'button';
      filterButton.dataset.thesisSelectGroup = thesisValue(group.id);
      nameCell.appendChild(filterButton);
      thesisCell(row, 'Status', '', thesisValue(group.status));
      thesisCell(
        row, 'Bull / bear / neutral', 'tabular',
        thesisNumber(group.long_count, 'count') + ' / ' +
        thesisNumber(group.short_count, 'count') + ' / ' +
        thesisNumber(group.neutral_count, 'count')
      );
      thesisCell(row, 'Top score', 'tabular', thesisNumber(group.max_opportunity));
      thesisCell(row, 'Contradiction', 'tabular', thesisNumber(group.max_contradiction));
      thesisCell(row, 'Last evaluated', 'thesis-date tabular', thesisDate(group.last_evaluation));
      target.appendChild(row);
    });
  }

  function renderThesisFalsificationQueue(root, jobs) {
    var target = root.querySelector('[data-thesis-falsification-queue]');
    if (!target) return;
    target.textContent = '';
    var falsificationJobs = thesisArray(jobs);
    if (!falsificationJobs.length) {
      target.appendChild(thesisElement(
        'li', 'thesis-state-cell',
        'No falsification jobs are queued. Absence of a run is not a pass; inspect dossier invalidation conditions.'
      ));
      return;
    }
    falsificationJobs.forEach(function (job) {
      var item = thesisElement('li', 'thesis-falsification-row');
      thesisAppendText(item, 'span', 'inv-status-pill', thesisValue(job.state));
      thesisAppendText(item, 'strong', '', 'Evidence + falsification cycle');
      thesisAppendText(item, 'span', 'thesis-date tabular', thesisDate(job.created_at));
      thesisAppendText(
        item, 'span', 'thesis-job-meta',
        'attempt ' + thesisNumber(job.attempt_count, 'count') + ' / ' + thesisNumber(job.max_attempts, 'count')
      );
      target.appendChild(item);
    });
  }

  function thesisLatestVersion(dossier) {
    var versions = thesisArray(dossier.versions);
    return versions.length ? thesisObject(versions[0]) : {};
  }

  function thesisField(root, key, value) {
    var node = root.querySelector('[data-thesis-field="' + key + '"]');
    if (node) node.textContent = thesisValue(value);
  }

  function renderThesisScenarios(root, scenarios) {
    var target = root.querySelector('[data-thesis-scenarios]');
    if (!target) return;
    target.textContent = '';
    var ordered = scenarios.slice().sort(function (left, right) {
      function order(item) {
        var name = String(item.name || '').toLowerCase();
        if (name.indexOf('bull') >= 0) return 0;
        if (item.is_base_case || name.indexOf('base') >= 0) return 1;
        if (name.indexOf('bear') >= 0) return 2;
        return 3;
      }
      return order(left) - order(right);
    });
    if (!ordered.length) {
      thesisStateRow(target, 5, 'No scenarios are available. Probability and expected return remain unknown.');
      return;
    }
    ordered.forEach(function (scenario) {
      var row = thesisElement('tr');
      thesisCell(row, 'Scenario', 'thesis-scenario-name', thesisValue(scenario.name));
      thesisCell(row, 'Probability', 'tabular', thesisNumber(scenario.probability, 'return'));
      thesisCell(row, 'Expected return', 'tabular', thesisNumber(scenario.expected_return, 'return'));
      thesisCell(row, 'Path and assumptions', '', thesisValue(scenario.description || scenario.assumptions || scenario.path));
      thesisCell(row, 'Version', 'tabular', thesisValue(scenario.version));
      target.appendChild(row);
    });
  }

  function normalizeEvidenceRelationship(value) {
    var relationship = String(value || 'context').toLowerCase();
    if (relationship.indexOf('support') >= 0) return 'supports';
    if (relationship.indexOf('contradict') >= 0 || relationship.indexOf('counter') >= 0) return 'contradicts';
    if (relationship.indexOf('invalid') >= 0 || relationship.indexOf('falsif') >= 0) return 'invalidation';
    return 'context';
  }

  function renderThesisEvidence(root, evidence) {
    var grouped = {supports: [], contradicts: [], invalidation: [], context: []};
    evidence.forEach(function (item) {
      grouped[normalizeEvidenceRelationship(item.relationship || item.evidence_role || item.role)].push(item);
    });
    Object.keys(grouped).forEach(function (relationship) {
      var section = root.querySelector('[data-evidence-relationship="' + relationship + '"]');
      if (!section) return;
      var count = section.querySelector('[data-evidence-count]');
      var list = section.querySelector('[data-evidence-list]');
      if (count) count.textContent = String(grouped[relationship].length);
      if (!list) return;
      list.textContent = '';
      if (!grouped[relationship].length) {
        list.appendChild(thesisElement('li', 'thesis-state-cell', 'No ' + relationship + ' evidence recorded.'));
        return;
      }
      grouped[relationship].forEach(function (item) {
        var entry = thesisElement('li', 'thesis-evidence-entry');
        thesisAppendText(
          entry, 'p', 'thesis-evidence-claim',
          thesisValue(thesisFirstDefined(item.claim, item.text, item.summary, item.excerpt, item.description))
        );
        var sourceAt = thesisFirstDefined(
          item.source_timestamp, item.published_at, item.observed_at, item.created_at
        );
        thesisAppendText(
          entry, 'span', 'thesis-evidence-meta tabular',
          'source ' + thesisDate(sourceAt) +
          ' · available ' + thesisDate(item.available_at) +
          ' · weight ' + thesisNumber(thesisFirstDefined(item.effective_weight, item.strength, item.weight)) +
          ' · quality ' + thesisNumber(item.quality_score) +
          ' · entailment ' + thesisNumber(item.entailment_score) +
          ' · freshness ' + thesisNumber(item.freshness_score)
        );
        var reference = thesisFirstDefined(
          item.provenance_ref,
          item.origin_key,
          item.source_ref,
          item.evidence_fingerprint,
          item.evidence_id,
          item.source_id,
          item.document_id,
          item.id
        );
        thesisAppendText(
          entry, 'span', 'thesis-provenance-ref',
          thesisValue(item.source_family) +
          ' · independent source ' + thesisValue(item.independence_key) +
          ' · provenance ' + thesisValue(reference)
        );
        list.appendChild(entry);
      });
    });
  }

  function renderThesisEventList(target, items, emptyText, kind) {
    if (!target) return;
    target.textContent = '';
    if (!items.length) {
      target.appendChild(thesisElement('p', 'thesis-state-cell', emptyText));
      return;
    }
    items.forEach(function (item) {
      var row = thesisElement('article', 'thesis-timeline-row');
      thesisAppendText(
        row, 'strong', '',
        thesisValue(thesisFirstDefined(item.forecast_key, item.name, item.metric, item.forecast, item.title, item.status, kind))
      );
      thesisAppendText(
        row, 'span', 'thesis-date tabular',
        thesisDate(thesisFirstDefined(item.measured_at, item.resolved_at, item.evaluated_at, item.target_date, item.as_of, item.created_at))
      );
      thesisAppendText(
        row, 'p', '',
        thesisValue(thesisFirstDefined(item.description, item.notes, item.outcome, item.result, item.actual_value, item.target_value, item.value, item.expected_value))
      );
      target.appendChild(row);
    });
  }

  function renderThesisRisks(root, risks) {
    var target = root.querySelector('[data-thesis-risks]');
    if (!target) return;
    target.textContent = '';
    var rows = thesisArray(risks).slice(0, 50);
    if (!rows.length) {
      target.appendChild(thesisElement('li', 'thesis-state-cell', 'No structured risks recorded.'));
      return;
    }
    rows.forEach(function (risk) {
      var item = thesisElement('li', 'thesis-risk-row');
      var riskKind = thesisFirstDefined(risk.kind, risk.risk_type, risk.category, 'risk');
      var severity = risk.severity;
      var riskLabel = thesisValue(riskKind);
      if (severity !== null && severity !== undefined && severity !== '') {
        riskLabel += ' · ' + thesisValue(severity);
      }
      thesisAppendText(item, 'strong', '', riskLabel);
      thesisAppendText(
        item,
        'span',
        '',
        thesisValue(thesisFirstDefined(risk.description, risk.condition, risk.name))
      );
      target.appendChild(item);
    });
  }

  function renderThesisInvalidations(root, dossier, core, version) {
    var list = root.querySelector('[data-thesis-invalidations]');
    var risks = thesisArray(dossier.risks);
    var conditions = thesisArray(
      version.invalidation_conditions || core.invalidation_conditions || core.invalidation_criteria
    );
    risks.forEach(function (risk) {
      if (String(risk.kind || risk.risk_type || '').toLowerCase().indexOf('invalid') >= 0) {
        conditions.push(risk.description || risk.condition || risk.name);
      }
    });
    if (list) {
      list.textContent = '';
      if (!conditions.length) list.appendChild(thesisElement('li', '', 'No invalidation conditions recorded.'));
      conditions.forEach(function (condition) {
        var value = typeof condition === 'object'
          ? condition.description || condition.condition || condition.name
          : condition;
        list.appendChild(thesisElement('li', '', thesisValue(value)));
      });
    }
    var latest = thesisArray(dossier.falsification_runs)[0];
    var state = root.querySelector('[data-thesis-invalidation-state]');
    if (state) {
      state.textContent = latest
        ? thesisValue(latest.status || latest.result) + ' · ' + thesisDate(latest.completed_at || latest.created_at)
        : 'Not yet tested';
    }
  }

  function thesisStructuredFindings(value) {
    if (Array.isArray(value)) return value.slice(0, 20);
    if (value && typeof value === 'object') return [value];
    if (typeof value === 'string') {
      try {
        var parsed = JSON.parse(value);
        return Array.isArray(parsed) ? parsed.slice(0, 20) : [parsed];
      } catch (error) {
        return value.trim() ? [value] : [];
      }
    }
    return [];
  }

  function thesisFindingLines(finding) {
    if (!finding || typeof finding !== 'object') return [thesisValue(finding)];
    var lines = [];
    thesisArray(finding.invalidation_ids).forEach(function (value) {
      lines.push('Invalidating evidence: ' + thesisValue(value));
    });
    thesisArray(finding.breached_condition_ids).forEach(function (value) {
      lines.push('Breached condition: ' + thesisValue(value));
    });
    thesisArray(finding.runner_findings).forEach(function (value) {
      value = thesisObject(value);
      lines.push(
        thesisValue(value.kind) + ': ' + thesisValue(value.statement) +
        (thesisArray(value.citations).length
          ? ' · citations ' + thesisArray(value.citations).join(', ')
          : '')
      );
    });
    thesisArray(finding.citation_failures).forEach(function (value) {
      value = thesisObject(value);
      lines.push(
        'Citation ' + thesisValue(value.reason) +
        (value.claim_id ? ' · claim ' + thesisValue(value.claim_id) : '') +
        (thesisArray(value.refs).length ? ' · refs ' + thesisArray(value.refs).join(', ') : '')
      );
    });
    thesisArray(finding.required_data).forEach(function (value) {
      value = thesisObject(value);
      lines.push('Required ' + thesisValue(value.kind) + ': ' + thesisValue(value.detail));
    });
    if (finding.runner_error) lines.push('Runner error: ' + thesisValue(finding.runner_error));
    return lines;
  }

  function renderThesisFalsification(root, runs) {
    var target = root.querySelector('[data-thesis-falsification]');
    if (!target) return;
    target.textContent = '';
    if (!runs.length) {
      target.appendChild(thesisElement('li', 'thesis-state-cell', 'No challenge or falsification runs recorded.'));
      return;
    }
    runs.forEach(function (run) {
      var item = thesisElement('li', 'thesis-challenge-row');
      thesisAppendText(item, 'time', 'thesis-date tabular', thesisDate(run.completed_at || run.started_at || run.created_at));
      thesisAppendText(item, 'strong', '', thesisValue(run.challenge_type || run.run_type || run.status));
      var summary = thesisFirstDefined(run.summary, run.result, run.challenge);
      if (summary !== null && summary !== undefined && summary !== '') {
        thesisAppendText(item, 'p', '', thesisValue(summary));
      }
      var findings = thesisStructuredFindings(run.findings);
      if (!findings.length && (summary === null || summary === undefined || summary === '')) {
        thesisAppendText(item, 'p', '', thesisValue(run.status));
      }
      findings.forEach(function (finding) {
        if (!finding || typeof finding !== 'object') {
          thesisAppendText(item, 'p', 'thesis-finding-summary', thesisValue(finding));
          return;
        }
        var findingBlock = thesisElement('div', 'thesis-finding');
        thesisAppendText(
          findingBlock,
          'strong',
          'thesis-finding-summary',
          thesisValue(finding.state) + ' · priority ' +
            thesisValue(finding.recommended_priority) + ' · contradiction ' +
            thesisNumber(finding.contradiction_strength)
        );
        var lines = thesisFindingLines(finding);
        if (lines.length) {
          var list = thesisElement('ul', 'rs-invalidation-list');
          lines.forEach(function (line) {
            list.appendChild(thesisElement('li', '', line));
          });
          findingBlock.appendChild(list);
        }
        item.appendChild(findingBlock);
      });
      target.appendChild(item);
    });
  }

  function renderThesisPositions(root, positions) {
    var target = root.querySelector('[data-thesis-positions]');
    if (!target) return;
    target.textContent = '';
    if (!positions.length) {
      target.appendChild(thesisElement('li', 'thesis-state-cell', 'No linked positions. This dossier remains research-only.'));
      return;
    }
    positions.forEach(function (position) {
      var item = thesisElement('li', 'thesis-position-row');
      var symbol = position.symbol || position.asset_symbol || position.instrument;
      if (symbol) {
        var link = thesisElement('a', '', thesisValue(symbol));
        link.href = '/assets/' + encodeURIComponent(String(symbol));
        item.appendChild(link);
      } else {
        thesisAppendText(item, 'span', 'tabular', thesisValue(position.position_id || position.name || position.id));
      }
      thesisAppendText(
        item, 'span', 'thesis-job-meta',
        thesisValue(position.link_type || position.status || position.relationship || position.position_type)
      );
      target.appendChild(item);
    });
  }

  function renderThesisCatalysts(root, catalysts) {
    var target = root.querySelector('[data-thesis-catalysts]');
    if (!target) return;
    target.textContent = '';
    if (!catalysts.length) {
      target.appendChild(thesisElement('li', 'thesis-state-cell', 'No dated catalysts recorded.'));
      return;
    }
    catalysts.forEach(function (catalyst) {
      var item = thesisElement('li', 'thesis-catalyst-row');
      thesisAppendText(item, 'span', 'inv-status-pill', thesisValue(catalyst.state));
      thesisAppendText(item, 'span', '', thesisValue(catalyst.description));
      thesisAppendText(item, 'time', 'thesis-date tabular', thesisDate(catalyst.expected_at));
      target.appendChild(item);
    });
  }

  function renderThesisScoreHistory(root, snapshots) {
    var target = root.querySelector('[data-thesis-score-history]');
    if (!target) return;
    target.textContent = '';
    if (!snapshots.length) {
      target.appendChild(thesisElement('p', 'thesis-state-cell', 'No prior score snapshots recorded.'));
      return;
    }
    snapshots.slice(0, 8).forEach(function (snapshot) {
      var row = thesisElement('div', 'thesis-score-history-row');
      thesisAppendText(row, 'time', 'thesis-date tabular', thesisDate(snapshot.captured_at));
      thesisAppendText(row, 'span', 'tabular', 'score ' + thesisNumber(snapshot.opportunity_score));
      thesisAppendText(
        row, 'span', 'tabular',
        'EV ' + thesisNumber(snapshot.expected_value, 'return') +
        ' / downside ' + thesisNumber(snapshot.expected_shortfall, 'return')
      );
      target.appendChild(row);
    });
  }

  function renderThesisVersions(root, versions) {
    var target = root.querySelector('[data-thesis-versions]');
    if (!target) return;
    target.textContent = '';
    if (!versions.length) {
      target.appendChild(thesisElement('li', 'thesis-state-cell', 'No version history recorded.'));
      return;
    }
    versions.forEach(function (version) {
      var item = thesisElement('li', 'thesis-version-row');
      thesisAppendText(item, 'strong', 'tabular', 'v' + thesisValue(version.version));
      thesisAppendText(item, 'time', 'thesis-date tabular', thesisDate(version.created_at));
      thesisAppendText(item, 'p', '', thesisValue(version.claim));
      thesisAppendText(item, 'span', 'thesis-job-meta', thesisValue(version.rationale) + ' · ' + thesisValue(version.changed_by));
      target.appendChild(item);
    });
  }

  function renderPlaybookConditions(parent, label, values) {
    var group = thesisElement('div', 'thesis-playbook-condition');
    thesisAppendText(group, 'dt', '', label);
    var detail = thesisElement('dd');
    var items = thesisArray(values);
    if (!items.length) {
      detail.textContent = thesisUnknown;
    } else {
      var list = thesisElement('ul');
      items.forEach(function (value) {
        list.appendChild(thesisElement('li', '', thesisValue(value)));
      });
      detail.appendChild(list);
    }
    group.appendChild(detail);
    parent.appendChild(group);
  }

  function renderPlaybookScenario(parent, label, scenario) {
    var row = thesisElement('div', 'thesis-playbook-scenario');
    var values = thesisObject(scenario);
    thesisAppendText(row, 'dt', '', label);
    thesisAppendText(
      row,
      'dd',
      'tabular',
      'probability ' + thesisNumber(values.probability, 'return') +
      ' · fractional return ' + thesisNumber(values.expected_return)
    );
    parent.appendChild(row);
  }

  function renderThesisPlaybooks(root, playbooks) {
    var target = root.querySelector('[data-thesis-playbooks]');
    if (!target) return;
    target.textContent = '';
    if (!playbooks.length) {
      target.appendChild(thesisElement(
        'p',
        'thesis-state-cell',
        'No catalyst playbooks are attached to this thesis.'
      ));
      return;
    }
    playbooks.forEach(function (playbook) {
      var entry = thesisElement('details', 'thesis-playbook-entry');
      if (playbook.superseded_at === null || playbook.superseded_at === undefined) {
        entry.open = true;
      }
      var summary = thesisElement('summary');
      var summaryText = thesisElement('span', 'thesis-playbook-summary');
      thesisAppendText(summaryText, 'strong', '', thesisValue(playbook.catalyst));
      thesisAppendText(
        summaryText,
        'span',
        'thesis-playbook-meta tabular',
        thesisValue(playbook.horizon) + ' · expected ' + thesisDate(playbook.expected_at)
      );
      summary.appendChild(summaryText);
      thesisAppendText(
        summary,
        'span',
        'inv-status-pill',
        playbook.superseded_at ? 'superseded' : 'active'
      );
      entry.appendChild(summary);

      var body = thesisElement('div', 'thesis-playbook-body');
      var identity = thesisElement('div', 'thesis-playbook-identity tabular');
      thesisAppendText(identity, 'span', '', 'playbook v' + thesisValue(playbook.version));
      thesisAppendText(identity, 'span', '', 'thesis v' + thesisValue(playbook.thesis_version));
      thesisAppendText(identity, 'span', '', 'created ' + thesisDate(playbook.created_at));
      if (playbook.superseded_at) {
        thesisAppendText(identity, 'span', '', 'superseded ' + thesisDate(playbook.superseded_at));
      }
      body.appendChild(identity);

      var eventTypes = thesisElement('div', 'thesis-playbook-event-types');
      thesisAppendText(eventTypes, 'span', 'label', 'Event types');
      thesisAppendText(
        eventTypes,
        'span',
        '',
        thesisArray(playbook.event_types).length
          ? thesisArray(playbook.event_types).join(' · ')
          : thesisUnknown
      );
      body.appendChild(eventTypes);

      var conditions = thesisElement('dl', 'thesis-playbook-conditions');
      renderPlaybookConditions(conditions, 'Trigger', playbook.trigger_conditions);
      renderPlaybookConditions(conditions, 'Confirmation', playbook.confirmation_conditions);
      renderPlaybookConditions(conditions, 'Invalidation', playbook.invalidation_conditions);
      body.appendChild(conditions);

      var scenarios = thesisElement('dl', 'thesis-playbook-scenarios');
      renderPlaybookScenario(scenarios, 'Bull', playbook.bull_scenario);
      renderPlaybookScenario(scenarios, 'Base', playbook.base_scenario);
      renderPlaybookScenario(scenarios, 'Bear', playbook.bear_scenario);
      body.appendChild(scenarios);

      var references = thesisElement('div', 'thesis-playbook-refs');
      thesisAppendText(references, 'span', 'label', 'Cited evidence');
      var cited = thesisArray(playbook.cited_evidence_refs);
      thesisAppendText(references, 'span', '', cited.length ? cited.join(' · ') : thesisUnknown);
      body.appendChild(references);
      entry.appendChild(body);
      target.appendChild(entry);
    });
  }

  function playbookAssessmentText(value) {
    if (typeof value === 'string') return thesisValue(value);
    var assessment = thesisObject(value);
    var preferred = thesisFirstDefined(
      assessment.summary,
      assessment.assessment,
      assessment.rationale,
      assessment.verdict
    );
    if (preferred !== null) return thesisValue(preferred);
    if (!Object.keys(assessment).length) return thesisUnknown;
    try {
      return JSON.stringify(assessment);
    } catch (_error) {
      return thesisUnknown;
    }
  }

  function renderThesisPlaybookMatches(root, matches) {
    var target = root.querySelector('[data-thesis-playbook-matches]');
    if (!target) return;
    target.textContent = '';
    if (!matches.length) {
      thesisStateRow(target, 5, 'No events have been matched to these playbooks.');
      return;
    }
    matches.forEach(function (match) {
      var row = thesisElement('tr');
      var eventCell = thesisCell(
        row,
        'Event type',
        'thesis-playbook-event',
        thesisValue(match.event_type)
      );
      thesisAppendText(
        eventCell,
        'span',
        'thesis-provenance-ref tabular',
        'event ' + thesisValue(match.event_id)
      );
      thesisCell(row, 'Source', '', thesisValue(match.source));
      thesisCell(row, 'Observed', 'thesis-date tabular', thesisDate(match.observed_at));
      thesisCell(row, 'Assigned kind', '', thesisValue(match.kind));
      var assessmentCell = thesisCell(
        row,
        'Assessment / cited refs',
        '',
        playbookAssessmentText(match.assessment)
      );
      thesisAppendText(
        assessmentCell,
        'span',
        'thesis-provenance-ref',
        'Cited refs ' +
          (thesisArray(match.evidence_refs).length
            ? thesisArray(match.evidence_refs).join(' · ')
            : thesisUnknown)
      );
      target.appendChild(row);
    });
  }

  function renderThesisCitationMap(root, citationMap) {
    var target = root.querySelector('[data-thesis-citation-map]');
    if (!target) return;
    target.textContent = '';
    citationMap = thesisObject(citationMap);
    var fields = ['claim', 'consensus', 'variant_perception', 'mechanism', 'catalyst', 'trend', 'valuation', 'sentiment'];
    fields.forEach(function (field) {
      var refs = thesisArray(citationMap[field]);
      var item = thesisElement('li');
      thesisAppendText(item, 'strong', '', field.replace(/_/g, ' ') + ': ');
      thesisAppendText(
        item,
        'span',
        refs.length ? 'thesis-provenance-ref tabular' : 'thesis-state-cell',
        refs.length ? refs.join(' · ') : 'missing'
      );
      target.appendChild(item);
    });
  }

  function renderThesisDetail(root, response) {
    var dossier = thesisObject(response.thesis);
    var core = thesisObject(dossier.thesis);
    var version = thesisLatestVersion(dossier);
    thesisField(root, 'claim', core.claim || core.thesis || core.title || core.name);
    thesisField(root, 'group', thesisArray(dossier.groups).map(function (group) { return group.name; }).join(' · ') || 'Ungrouped thesis');
    thesisField(root, 'status', core.status);
    thesisField(root, 'id', core.id || root.dataset.thesisId);
    thesisField(root, 'version', version.version || core.current_version);
    thesisField(root, 'direction', core.direction);
    thesisField(root, 'horizon', core.horizon);
    thesisField(root, 'evaluated', thesisDate(core.last_evaluated_at || version.created_at));
    thesisField(root, 'mechanism', core.mechanism);
    thesisField(root, 'variant', version.variant_perception || core.variant_perception || core.direction);
    thesisField(root, 'origin', core.origin);
    thesisField(root, 'catalyst-summary', core.catalyst_summary);
    thesisField(root, 'trend-context', version.trend_context || core.trend_context);
    thesisField(root, 'valuation-context', version.valuation_context || core.valuation_context);
    thesisField(root, 'sentiment-context', version.sentiment_context || core.sentiment_context);
    renderThesisCitationMap(root, version.citation_map || core.citation_map);
    root.querySelectorAll('[data-score]').forEach(function (node) {
      var key = node.dataset.score;
      var value = core[key];
      if (value === undefined) value = version[key];
      node.textContent = thesisNumber(value, key === 'expected_value' || key === 'expected_shortfall' ? 'return' : '');
    });
    renderThesisScenarios(root, thesisArray(dossier.scenarios));
    renderThesisPlaybooks(root, thesisArray(dossier.playbooks));
    renderThesisPlaybookMatches(root, thesisArray(dossier.playbook_matches));
    renderThesisEvidence(root, thesisArray(dossier.evidence));
    renderThesisEventList(
      root.querySelector('[data-thesis-forecasts]'),
      thesisArray(dossier.forecasts),
      'No forecasts recorded.',
      'forecast'
    );
    renderThesisEventList(
      root.querySelector('[data-thesis-outcomes]'),
      thesisArray(dossier.outcomes),
      'No outcomes resolved.',
      'outcome'
    );
    renderThesisCatalysts(root, thesisArray(dossier.catalysts));
    renderThesisScoreHistory(root, thesisArray(dossier.opportunity_snapshots));
    renderThesisVersions(root, thesisArray(dossier.versions));
    renderThesisInvalidations(root, dossier, core, version);
    renderThesisRisks(root, thesisArray(dossier.risks));
    renderThesisFalsification(root, thesisArray(dossier.falsification_runs));
    renderThesisPositions(root, thesisArray(dossier.positions));
  }

  function thesisRunMessage(error) {
    if (error.status === 429) return 'A research cycle is already bounded or rate-limited. Refresh the desk before retrying.';
    if (error.status === 503) return 'Research infrastructure is unavailable. No cycle was queued.';
    return 'The research cycle could not be queued.';
  }

  function initThesisView(root) {
    if (root.dataset.thesisInitialized === 'true') return;
    root.dataset.thesisInitialized = 'true';
    var controller = null;
    var boundedRefreshTimer = null;

    function filters() {
      var group = root.querySelector('[data-thesis-group-filter]');
      var score = root.querySelector('[data-thesis-score-filter]');
      var status = root.querySelector('[data-thesis-status-filter]');
      return {
        group: group ? group.value : '',
        score: score ? score.value : thesisDefaultMinimumScore,
        status: status ? status.value : ''
      };
    }

    function refresh() {
      if (controller) controller.abort();
      controller = new AbortController();
      var signal = controller.signal;
      thesisSetBusy(root, true);
      thesisMessage(root, root.dataset.thesisView === 'detail' ? 'Loading dossier…' : 'Refreshing bounded research data…', 'loading');
      if (root.dataset.thesisView === 'detail') {
        thesisFetch(
          '/api/research/theses/' + encodeURIComponent(root.dataset.thesisId),
          {},
          signal
        ).then(function (body) {
          renderThesisDetail(root, body);
          thesisMessage(root, 'Dossier loaded. Evidence and history are bounded to the API response.', 'ready');
        }).catch(function (error) {
          if (error.name === 'AbortError') return;
          thesisMarkDetailUnavailable(
            root,
            error.status === 404 ? 'Thesis not found.' : 'Thesis dossier unavailable.'
          );
          thesisMessage(
            root,
            error.status === 404 ? 'Thesis not found.' : 'Thesis dossier unavailable. No score or missing value has been inferred.',
            'unavailable'
          );
        }).finally(function () {
          if (controller && controller.signal === signal) thesisSetBusy(root, false);
        });
        return;
      }

      var selected = filters();
      var opportunityQuery = new URLSearchParams({limit: root.dataset.thesisView === 'research-preview' ? '3' : '50'});
      if (selected.group) opportunityQuery.set('group_id', selected.group);
      opportunityQuery.set('minimum_score', selected.score);
      if (selected.score === '0') opportunityQuery.set('include_ineligible', 'true');
      var groupQuery = new URLSearchParams({limit: root.dataset.thesisView === 'research-preview' ? '4' : '50'});
      if (root.dataset.thesisView === 'research-preview') {
        groupQuery.set('status', 'active');
      } else if (selected.status) {
        groupQuery.set('status', selected.status);
      }
      Promise.allSettled([
        thesisFetch('/api/research/theses/status', {}, signal),
        thesisFetch('/api/research/theses/groups?' + groupQuery.toString(), {}, signal),
        thesisFetch('/api/research/theses/opportunities?' + opportunityQuery.toString(), {}, signal)
      ]).then(function (results) {
        if (signal.aborted) return;
        var status = results[0].status === 'fulfilled' ? results[0].value : {};
        var groups = results[1].status === 'fulfilled' ? thesisArray(results[1].value.groups) : null;
        var opportunities = results[2].status === 'fulfilled' ? thesisArray(results[2].value.opportunities) : null;
        var failures = results.filter(function (result) { return result.status === 'rejected'; }).length;
        renderThesisStatus(root, status, groups, opportunities);
        var groupRows = thesisArray(groups);
        var opportunityRows = thesisArray(opportunities);
        populateThesisGroupFilter(root, groupRows);
        renderThesisGroups(root, groupRows);
        renderThesisOpportunities(root, opportunityRows, selected.score);
        renderThesisFalsificationQueue(root, status.autonomy_jobs);
        if (results[0].status === 'rejected') {
          thesisCollectionState(root, '[data-thesis-falsification-queue]', 1, 'Falsification state unavailable.');
        }
        if (results[1].status === 'rejected') {
          thesisCollectionState(root, '[data-thesis-groups]', 6, 'Tournament groups unavailable.');
        }
        if (results[2].status === 'rejected') {
          thesisCollectionState(root, '[data-thesis-opportunities]', 8, 'Ranked opportunities unavailable.');
        }
        if (failures === 3 || status.available === false) {
          thesisMessage(root, 'Research desk unavailable. Existing filing and research workspaces remain available.', 'unavailable');
          root.querySelectorAll('.thesis-state-cell').forEach(function (node) {
            node.textContent = 'Research desk unavailable.';
          });
        } else if (failures) {
          thesisMessage(root, 'Partial research data. Available sections are shown; unknown values remain \u2014.', 'partial');
        } else if (thesisSourcesPartial(status)) {
          thesisMessage(root, 'Research data loaded with partial or unavailable source feeds. Unknown values remain \u2014.', 'partial');
        } else if (!opportunityRows.length) {
          thesisMessage(root, thesisOpportunityEmptyMessage(selected.score), 'empty');
        } else {
          thesisMessage(root, 'Research desk current as of this refresh.', 'ready');
        }
      }).finally(function () {
        if (controller && controller.signal === signal) thesisSetBusy(root, false);
      });
    }

    root.addEventListener('change', function (event) {
      if (event.target.matches('[data-thesis-group-filter], [data-thesis-score-filter], [data-thesis-status-filter]')) refresh();
    });
    root.addEventListener('click', function (event) {
      var refreshButton = event.target.closest('[data-thesis-refresh]');
      if (refreshButton) {
        refresh();
        return;
      }
      var groupButton = event.target.closest('[data-thesis-select-group]');
      if (groupButton) {
        var select = root.querySelector('[data-thesis-group-filter]');
        if (select) {
          select.value = groupButton.dataset.thesisSelectGroup;
          select.focus();
          refresh();
        }
        return;
      }
      var runButton = event.target.closest('[data-thesis-run]');
      if (!runButton) return;
      runButton.disabled = true;
      runButton.setAttribute('aria-busy', 'true');
      thesisMessage(root, 'Queueing one bounded research cycle…', 'loading');
      thesisFetch('/api/research/theses/run', {
        method: 'POST',
        headers: csrfHeaders({'Content-Type': 'application/json'}),
        body: JSON.stringify({force: false})
      }).then(function (body) {
        thesisMessage(
          root,
          body.status === 'already_queued'
            ? 'A bounded research cycle is already queued. No duplicate was created.'
            : 'Research cycle queued for analysis only. The desk will refresh once.',
          'queued'
        );
        window.clearTimeout(boundedRefreshTimer);
        boundedRefreshTimer = window.setTimeout(refresh, 2500);
      }).catch(function (error) {
        thesisMessage(root, thesisRunMessage(error), 'unavailable');
      }).finally(function () {
        runButton.disabled = false;
        runButton.setAttribute('aria-busy', 'false');
      });
    });
    refresh();
  }

  function initThesisViews(root) {
    var scope = root || document;
    if (scope.matches && scope.matches('[data-thesis-view]')) initThesisView(scope);
    scope.querySelectorAll('[data-thesis-view]').forEach(initThesisView);
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
    initThesisViews(document);
  });

  ['htmx:afterSwap', 'htmx:afterSettle'].forEach(function (eventName) {
    document.body.addEventListener(eventName, function (evt) {
      if (evt.detail && evt.detail.target) initCharts(evt.detail.target);
      if (evt.detail && evt.detail.target) initDynamicUi(evt.detail.target);
    });
  });
})();
