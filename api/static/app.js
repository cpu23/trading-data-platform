(function () {
  'use strict';

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
      if (e.target.closest('.symbol-link')) return;
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
          var datasets = [];
          results.forEach(function(data, i) {
            if (!data || !data.observations || !data.observations.length) return;
            datasets.push({
              label: indicators[i],
              data: data.observations.map(function(o) {
                var date = new Date(o.observed_at);
                return {
                  x: isNaN(date.getTime()) ? o.observed_at : date.toLocaleDateString('en-US', {month: 'short', day: 'numeric'}),
                  y: o.value
                };
              }),
              borderColor: colors[i],
              borderWidth: 1,
              pointRadius: 0,
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
            data: { datasets: datasets },
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
                  ticks: { color: '#6B6B66', font: { size: 10 } }
                }
              },
              plugins: {
                legend: {
                  display: true,
                  labels: { color: '#999992', font: { size: 11 }, usePointStyle: true }
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

  /* Run cycle → cycleComplete trigger --------------------------------------- */
  var spinnerFrames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];
  var spinnerTimer = null;

  function startBrailleSpinner(spinnerEl) {
    var frame = 0;
    if (!spinnerEl) return;
    stopBrailleSpinner(false);
    spinnerEl.textContent = spinnerFrames[frame];
    spinnerEl.classList.add('active');
    spinnerTimer = setInterval(function () {
      frame = (frame + 1) % spinnerFrames.length;
      spinnerEl.textContent = spinnerFrames[frame];
    }, 90);
  }

  function stopBrailleSpinner(clearText) {
    if (spinnerTimer) {
      clearInterval(spinnerTimer);
      spinnerTimer = null;
    }
    var spinnerEl = document.getElementById('cycle-spinner');
    if (spinnerEl) {
      spinnerEl.classList.remove('active');
      if (clearText !== false) spinnerEl.textContent = '';
    }
  }

  function initCycleButton() {
    function dispatchCycleRefresh() {
      document.body.dispatchEvent(new CustomEvent('cycleComplete', { bubbles: true }));
    }

    function triggerCycle(mode, budgetConfirmed) {
      var btn = document.getElementById('run-cycle-btn');
      var menu = document.getElementById('cycle-mode-select');
      if (!btn || btn.disabled) return;
      var cycleLabel = btn.querySelector('.btn-label');
      var originalLabel = btn.getAttribute('data-original-label') || 'Refresh';
      startBrailleSpinner(btn.querySelector('#cycle-spinner'));
      if (cycleLabel) cycleLabel.textContent = 'Starting ' + mode.replace('_', ' ') + '…';
      btn.disabled = true;
      if (menu) menu.disabled = true;

      fetch('/api/triggers/cycle', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mode: mode, budget_confirmed: budgetConfirmed})
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
        pollCycleCompletion(response.job_id, btn, cycleLabel, originalLabel, dispatchCycleRefresh);
      }).catch(function (error) {
        if (cycleLabel) cycleLabel.textContent = error.message;
        stopBrailleSpinner();
        btn.disabled = false;
        if (menu) menu.disabled = false;
        dispatchCycleRefresh();
      }).finally(function () {
        if (menu) menu.value = '';
      });
    }

    document.body.addEventListener('click', function (event) {
      if (!event.target.closest('#run-cycle-btn')) return;
      triggerCycle('refresh', false);
    });

    document.body.addEventListener('change', function (event) {
      if (event.target.id !== 'cycle-mode-select') return;
      var mode = event.target.value;
      if (!mode) return;
      if (mode === 'force_full') {
        if (!window.confirm(
          'Force full is a daily budget bypass and may incur additional cost. Continue?'
        )) {
          event.target.value = '';
          return;
        }
      }
      triggerCycle(mode, mode === 'force_full');
    });
  }

  function stageDetail(stage) {
    var detail = [];
    if (stage.records_fetched !== null && stage.records_fetched !== undefined) {
      detail.push(stage.records_fetched + ' fetched');
    }
    if (stage.records_written !== null && stage.records_written !== undefined) {
      detail.push(stage.records_written + ' written');
    }
    if (stage.tokens_input || stage.tokens_output) {
      detail.push(((stage.tokens_input || 0) + (stage.tokens_output || 0)) + ' tokens');
    }
    if (stage.cost_usd !== null && stage.cost_usd !== undefined) {
      detail.push('$' + Number(stage.cost_usd).toFixed(4));
    }
    if (stage.duration_ms) detail.push((stage.duration_ms / 1000).toFixed(1) + 's');
    if (stage.error || stage.error_message) detail.push(stage.error || stage.error_message);
    return detail.join(' · ');
  }

  function renderCycleProgress(data) {
    var panel = document.getElementById('cycle-progress');
    var headline = document.getElementById('cycle-progress-headline');
    var count = document.getElementById('cycle-progress-count');
    var list = document.getElementById('cycle-stage-list');
    if (!panel || !headline || !count || !list) return;

    var progress = data.progress || {};
    var snapshotStages = progress.stages || [];
    var loggedByComponent = {};
    (data.stages || []).forEach(function (stage) {
      loggedByComponent[stage.component] = stage;
    });
    var stages = snapshotStages.length
      ? snapshotStages.map(function (stage) {
          return Object.assign({}, stage, loggedByComponent[stage.component] || {});
        })
      : (data.stages || []);

    panel.hidden = false;
    var current = progress.current_stage;
    var terminal = ['success', 'partial', 'completed', 'failed'].includes(data.status);
    headline.textContent = terminal
      ? 'Cycle finished: ' + data.status
      : current
        ? 'Running ' + (progress.current_kind || 'stage') + ': ' + current.replaceAll('_', ' ')
        : 'Cycle running...';
    var completedCount = progress.completed_stages !== undefined
      ? progress.completed_stages
      : stages.filter(function (stage) {
          return !['pending', 'running'].includes(stage.status);
        }).length;
    count.textContent = completedCount + ' / ' +
      (progress.total_stages || stages.length) + ' stages';
    list.replaceChildren();

    stages.forEach(function (stage) {
      var item = document.createElement('div');
      var status = stage.status || 'pending';
      item.className = 'cycle-stage cycle-stage-' + status;
      var dot = document.createElement('span');
      dot.className = 'status-dot status-' + (
        status === 'success' ? 'success' :
        status === 'failed' ? 'failed' : 'partial'
      );
      var name = document.createElement('span');
      name.textContent = stage.component.replaceAll('_', ' ');
      var state = document.createElement('span');
      state.className = 'dim';
      state.textContent = status;
      var detail = document.createElement('span');
      detail.className = 'dim tabular';
      detail.textContent = stageDetail(stage);
      item.append(dot, name, state, detail);
      list.appendChild(item);
    });
  }

  function pollCycleCompletion(correlationId, btn, labelEl, originalLabel, dispatchCycleRefresh) {
    var maxAttempts = 100; // ~5 minutes at 3s intervals
    var attempts = 0;

    if (labelEl) labelEl.textContent = 'Running cycle...';
    if (btn) btn.disabled = true;

    function poll() {
      attempts++;
      if (attempts > maxAttempts) {
        if (labelEl) labelEl.textContent = 'Cycle taking longer than expected — check logs';
        stopBrailleSpinner();
        if (btn) btn.disabled = false;
        var timeoutMenu = document.getElementById('cycle-mode-select');
        if (timeoutMenu) timeoutMenu.disabled = false;
        dispatchCycleRefresh();
        return;
      }
      fetch('/api/system/cycle-status?correlation_id=' + encodeURIComponent(correlationId))
        .then(function (r) {
          if (!r.ok) throw new Error('Cycle status unavailable');
          return r.json();
        })
        .then(function (data) {
          renderCycleProgress(data);
          if (['success', 'partial', 'completed'].includes(data.status)) {
            if (labelEl) labelEl.textContent = originalLabel;
            stopBrailleSpinner();
            if (btn) btn.disabled = false;
            var completeMenu = document.getElementById('cycle-mode-select');
            if (completeMenu) completeMenu.disabled = false;
            dispatchCycleRefresh();
          } else if (data.status === 'failed') {
            if (labelEl) labelEl.textContent = 'Cycle failed — check logs';
            stopBrailleSpinner();
            if (btn) btn.disabled = false;
            var failedMenu = document.getElementById('cycle-mode-select');
            if (failedMenu) failedMenu.disabled = false;
            dispatchCycleRefresh();
          } else {
            setTimeout(poll, 2000);
          }
        })
        .catch(function (err) {
          console.error('Cycle status poll error', err);
          setTimeout(poll, 3000);
        });
    }

    poll();
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
        headers: {'Content-Type': 'application/json'},
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

  function initDynamicUi(root) {
    initCharts(root);
    initTimezoneControl(root);
  }

  /* Boot --------------------------------------------------------------------- */
  document.addEventListener('DOMContentLoaded', function () {
    initCharts(document);
    initModal();
    initCards();
    initIndicatorKeyboard();
    initExpansionPanelSync();
    initProvenance();
    initLiveQuotes();
    initLogs();
    initCycleButton();
    initTimezoneControl(document);
  });

  ['htmx:afterSwap', 'htmx:afterSettle'].forEach(function (eventName) {
    document.body.addEventListener(eventName, function (evt) {
      if (evt.detail && evt.detail.target) initCharts(evt.detail.target);
      if (evt.detail && evt.detail.target) initDynamicUi(evt.detail.target);
    });
  });
})();
