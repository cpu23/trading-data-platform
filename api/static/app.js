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
    setModalState('Loading series...', false);
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
                : createdAt.toLocaleString([], {day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit'});
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

    document.body.addEventListener('htmx:beforeRequest', function (evt) {
      var btn = evt.detail.elt;
      if (btn && btn.id === 'run-cycle-btn') {
        var cycleLabel = btn.querySelector('.btn-label');
        var cycleSpinner = btn.querySelector('#cycle-spinner');
        startBrailleSpinner(cycleSpinner);
        if (cycleLabel) cycleLabel.textContent = 'Running cycle...';
        btn.disabled = true;
      }
    });

    document.body.addEventListener('htmx:afterRequest', function (evt) {
      var btn = evt.detail.elt;
      if (btn && btn.id === 'run-cycle-btn') {
        var cycleLabel = btn.querySelector('.btn-label');
        var originalLabel = btn.getAttribute('data-original-label') || 'Run cycle';
        if (evt.detail.successful) {
          var response = JSON.parse(evt.detail.xhr.responseText);
          var correlationId = response.job_id;
          htmx.ajax('GET', '/partials/cards/clear', {target: '#expansion-panel', swap: 'outerHTML'});
          pollCycleCompletion(correlationId, btn, cycleLabel, originalLabel, dispatchCycleRefresh);
        } else {
          var status = evt.detail.xhr.status;
          console.error('Cycle request failed:', status, evt.detail.xhr.responseText);
          if (status === 409) {
            if (cycleLabel) cycleLabel.textContent = 'Cycle already running';
          } else {
            if (cycleLabel) cycleLabel.textContent = 'Cycle failed — try again';
          }
          stopBrailleSpinner();
          btn.disabled = false;
          dispatchCycleRefresh();
        }
      }
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
            dispatchCycleRefresh();
          } else if (data.status === 'failed') {
            if (labelEl) labelEl.textContent = 'Cycle failed — check logs';
            stopBrailleSpinner();
            if (btn) btn.disabled = false;
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

  /* Boot --------------------------------------------------------------------- */
  document.addEventListener('DOMContentLoaded', function () {
    initModal();
    initCards();
    initIndicatorKeyboard();
    initExpansionPanelSync();
    initProvenance();
    initLiveQuotes();
    initLogs();
    initCycleButton();
  });
})();
