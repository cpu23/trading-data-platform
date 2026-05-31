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

  /* Log row expand / collapse ----------------------------------------------- */
  function initLogs() {
    document.body.addEventListener('click', function (e) {
      var row = e.target.closest('#logs-table-body tr[data-log-id]');
      if (row) {
        var id = row.getAttribute('data-log-id');
        var detailRow = document.querySelector('#logs-table-body tr[data-detail-for="' + id + '"]');
        if (detailRow) {
          detailRow.classList.toggle('expanded');
        }
      }
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

  function pollCycleCompletion(correlationId, btn, labelEl, originalLabel, dispatchCycleRefresh) {
    var maxAttempts = 100; // ~5 minutes at 3s intervals
    var attempts = 0;

    if (labelEl) labelEl.textContent = 'Running cycle...';
    if (btn) btn.disabled = true;

    var interval = setInterval(function () {
      attempts++;
      if (attempts > maxAttempts) {
        clearInterval(interval);
        if (labelEl) labelEl.textContent = 'Cycle taking longer than expected — check logs';
        stopBrailleSpinner();
        if (btn) btn.disabled = false;
        dispatchCycleRefresh();
        return;
      }
      fetch('/api/system/cycle-status?correlation_id=' + encodeURIComponent(correlationId))
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.status === 'completed') {
            clearInterval(interval);
            if (labelEl) labelEl.textContent = originalLabel;
            stopBrailleSpinner();
            if (btn) btn.disabled = false;
            dispatchCycleRefresh();
          } else if (data.status === 'failed') {
            clearInterval(interval);
            if (labelEl) labelEl.textContent = 'Cycle failed — check logs';
            stopBrailleSpinner();
            if (btn) btn.disabled = false;
            dispatchCycleRefresh();
          }
        })
        .catch(function (err) {
          console.error('Cycle status poll error', err);
        });
    }, 3000);
  }

  /* Boot --------------------------------------------------------------------- */
  document.addEventListener('DOMContentLoaded', function () {
    initModal();
    initCards();
    initIndicatorKeyboard();
    initExpansionPanelSync();
    initLogs();
    initCycleButton();
  });
})();
