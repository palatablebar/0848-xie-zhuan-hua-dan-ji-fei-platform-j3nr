(function () {
  var state = { payload: null, kind: 'bars', run: null, approved: false, refundReasons: null };
  var $ = function (id) { return document.getElementById(id); };
  function esc(value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (ch) {
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch];
    });
  }
  async function request(url, options) {
    var response = await fetch(url, Object.assign({headers: {'Content-Type': 'application/json'}}, options || {}));
    var data = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
    return data;
  }
  function setNotice(text) { $('notice').textContent = text || ''; }
  function renderIssues(data) {
    $('issue-count').textContent = data.issues.length + ' 条';
    $('issue-list').innerHTML = data.issues.length ? data.issues.map(function (item) {
      return '<li><span>' + esc(item.line || '-') + '</span><span>' + esc(item.key || '无主键') + '</span><code>' + esc(item.code) + '</code></li>';
    }).join('') : '<li class="empty">暂无异常</li>';
  }
  function renderBreakdown(items) {
    if (!items || !items.length) return '';
    return '<details><summary>' + items.length + ' 项</summary><ul>' + items.map(function (item) {
      return '<li>' + esc(item) + '</li>';
    }).join('') + '</ul></details>';
  }
  function renderRows(data) {
    var labels = Object.assign({label: '记录'}, state.resultColumns);
    var source = data.row_kind === 'source';
    var hasBreakdown = data.rows.some(function (row) { return row.breakdown && row.breakdown.length; });
    var keys = source ? state.inputColumns : Object.keys(labels).filter(function (key) {
      return data.rows.some(function (row) { return Object.hasOwn(row, key); });
    });
    $('row-count').textContent = source ? data.rows.length + ' 行预览' :
      data.summary.PASS + ' 通过 / ' + data.summary.REVIEW + ' 复核 / ' + data.summary.REJECT + ' 阻断';
    var tail = source ? '' : '<th>结论</th>' + (hasBreakdown ? '<th>计算分项</th>' : '');
    $('row-head').innerHTML = '<tr>' + keys.map(function (key) { return '<th>' + esc(labels[key] || key) + '</th>'; }).join('') + tail + '</tr>';
    $('row-body').innerHTML = data.rows.length ? data.rows.map(function (row) {
      var values = keys.map(function (key) { return '<td class="' + (typeof row[key] === 'number' ? 'num' : '') + '">' + esc(row[key]) + '</td>'; }).join('');
      var verdict = source ? '' : '<td><span class="verdict" data-v="' + esc(row.verdict) + '">' + esc(row.verdict || '') + '</span><br><small>' + esc(row.reason || '') + '</small></td>';
      var breakdown = hasBreakdown ? '<td>' + renderBreakdown(row.breakdown) + '</td>' : '';
      return '<tr>' + values + verdict + breakdown + '</tr>';
    }).join('') : '<tr><td class="empty" colspan="' + (keys.length + (source ? 0 : 1)) + '">没有可展示条目</td></tr>';
  }
  function renderLedger(data) {
    if (!data.ledger) { $('ledger-list').innerHTML = '<p class="empty">运行对账后显示</p>'; return; }
    var values = data.ledger;
    $('ledger-list').innerHTML = [
      ['来源行', values.counts.input], ['采信行', values.counts.accepted], ['隔离行', values.counts.isolated],
      ['精确重复', values.counts.duplicates], ['忽略行', values.counts.ignored], ['计算条目', values.counts.computed]
    ].map(function (row) { return '<div class="ledger-row"><span>' + row[0] + '</span><strong>' + row[1] + '</strong></div>'; }).join('') +
      '<div class="ledger-row"><span>' + esc(values.metric_field) + '</span><strong>' + esc(values.metric_total) + '</strong></div>' +
      values.checks.map(function (check) { return '<div class="ledger-row"><span>' + esc(check.label) + '</span><strong class="' + (check.ok ? 'check-ok' : 'check-bad') + '">' + (check.ok ? '通过' : '异常') + '</strong></div>'; }).join('');
  }
  function reasonLabel(code) {
    return (state.refundReasons && state.refundReasons[code]) || code;
  }
  function renderRefund(data) {
    var refund = data.refund;
    if (!refund) {
      $('refund-summary').textContent = '--';
      $('refund-metrics').innerHTML = '<p class="empty">运行对账后显示</p>';
      $('refund-head').innerHTML = '';
      $('refund-body').innerHTML = '';
      $('refund-submit').disabled = true;
      return;
    }
    $('refund-summary').textContent = '已确认 ' + refund.confirmed_seconds + ' 秒 / 待补证 ' + refund.pending_seconds + ' 秒';
    var attribution = (refund.by_attribution || []).map(function (item) {
      return esc(item.label) + ' ' + item.seconds;
    }).join('，') || '无';
    $('refund-metrics').innerHTML = [
      ['已确认可退时长(秒)', refund.confirmed_seconds],
      ['待补证时长(秒)', refund.pending_seconds],
      ['可证多收合计(秒)', refund.total_overcharge_seconds],
      ['已确认可退金额(分)', refund.confirmed_amount_cents],
      ['按归属确认时长', attribution]
    ].map(function (row) { return '<div class="ledger-row"><span>' + row[0] + '</span><strong>' + row[1] + '</strong></div>'; }).join('') +
      (refund.pending_reasons || []).map(function (item) {
        return '<div class="ledger-row"><span>' + esc(item.label) + '</span><strong>' + item.count + '</strong></div>';
      }).join('') +
      (refund.checks || []).map(function (check) {
        return '<div class="ledger-row"><span>' + esc(check.label) + '</span><strong class="' + (check.ok ? 'check-ok' : 'check-bad') + '">' + (check.ok ? '通过' : '异常') + '</strong></div>';
      }).join('');
    $('refund-head').innerHTML = '<tr><th>话单号</th><th>归属</th><th>已计秒</th><th>已确认</th><th>待补证</th><th>状态</th><th>待补证原因</th><th>说明</th></tr>';
    var claims = refund.claims || [];
    $('refund-body').innerHTML = claims.length ? claims.map(function (claim) {
      var charged = (claim.versions || []).map(function (item) { return item.charged_seconds; });
      var chargedText = charged.length ? Array.from(new Set(charged)).join(' / ') : '--';
      var stateText = claim.status === 'CONFIRMED' ? '已确认' : '待补证';
      var reasons = (claim.pending_reasons || []).map(reasonLabel).join('；') || '—';
      var notes = (claim.note_texts || []).join('；') || '—';
      return '<tr><td>' + esc(claim.call_id) + '</td><td>' + esc(claim.attribution || (claim.attribution_disputed ? '未归属' : (claim.subscriber || '') + '|' + (claim.direction || ''))) +
        '</td><td class="num">' + esc(chargedText) + '</td><td class="num">' + claim.confirmed_seconds +
        '</td><td class="num">' + claim.pending_seconds + '</td><td><span class="refund-status-' + esc(claim.status) + '">' + stateText +
        '</span></td><td><small>' + esc(reasons) + '</small></td><td><small>' + esc(notes) + '</small></td></tr>';
    }).join('') : '<tr><td class="empty" colspan="8">没有可退时长</td></tr>';
    $('refund-submit').disabled = !state.run || !claims.some(function (claim) { return claim.pending_seconds > 0; });
  }
  function render(data) {
    state.payload = data;
    $('chart-label').textContent = Object.values(state.resultColumns)[0];
    $('m-input').textContent = data.counts.input;
    $('m-accepted').textContent = data.counts.accepted;
    $('m-isolated').textContent = data.counts.isolated + data.counts.duplicates;
    $('m-rejected').textContent = data.summary.REJECT;
    renderRows(data);
    renderIssues(data);
    renderLedger(data);
    renderRefund(data);
    window.renderChart($('chart'), data.chart && data.chart.length ? (state.kind || 'bars') : 'bars', data.chart || []);
  }
  function stageButtons(stages) {
    $('stage-nav').innerHTML = stages.map(function (stage, index) {
      return '<button type="button" data-stage="' + stage.id + '" aria-pressed="' + (index === 0 ? 'true' : 'false') + '">' + esc(stage.name) + '</button>';
    }).join('');
    Array.from(document.querySelectorAll('[data-stage]')).forEach(function (button) {
      button.addEventListener('click', function () {
        document.querySelectorAll('[data-stage]').forEach(function (other) { other.setAttribute('aria-pressed', String(other === button)); });
        loadStage(button.getAttribute('data-stage'));
      });
    });
  }
  async function loadStage(stage) {
    try {
      setNotice('');
      document.body.setAttribute('aria-busy', 'true');
      var payload = await request('/api/stages/' + stage, {method: 'POST', body: JSON.stringify({profile: $('profile').value})});
      render(payload);
    } catch (error) { setNotice(error.message); }
    finally { document.body.removeAttribute('aria-busy'); }
  }
  async function runBatch() {
    try {
      setNotice('');
      $('run').disabled = true;
      var run = await request('/api/runs', {method: 'POST', body: JSON.stringify({profile: $('profile').value})});
      applyRun(run);
    } catch (error) { setNotice(error.message); }
    finally { $('run').disabled = false; }
  }
  async function review(decision) {
    if (!state.run) return;
    try {
      var run = await request('/api/runs/' + state.run.run_id + '/review', {
        method: 'POST',
        body: JSON.stringify({decision: decision, comment: $('comment').value, expected_revision: state.run.revision})
      });
      applyRun(run);
    } catch (error) { setNotice(error.message); }
  }
  function applyRun(run) {
    state.run = run;
    state.approved = run.status === 'APPROVED';
    render(run);
    $('run-status').textContent = run.status + ' · rev ' + run.revision;
    $('revision').textContent = 'rev ' + run.revision;
    $('sign').disabled = run.status === 'APPROVED' || run.summary.REJECT > 0;
    $('hold').disabled = run.status === 'APPROVED';
    $('download').hidden = !state.approved;
    if (state.approved) $('download').href = '/api/runs/' + run.run_id + '/evidence';
  }
  async function submitRefund(event) {
    event.preventDefault();
    if (!state.run) return;
    var body = {
      call_id: $('refund-call').value.trim(),
      seconds: $('refund-seconds').value,
      subscriber: $('refund-subscriber').value.trim(),
      direction: $('refund-direction').value,
      evidence: $('refund-evidence').value.trim(),
      note: $('refund-note').value.trim()
    };
    try {
      setNotice('');
      var result = await request('/api/runs/' + state.run.run_id + '/refund', {method: 'POST', body: JSON.stringify(body)});
      setNotice(result.created ? '补证已入账' : '同一证据已入账，未重复计入');
      $('refund-form').reset();
      var run = await request('/api/runs/' + state.run.run_id);
      applyRun(run);
    } catch (error) { setNotice(error.message); }
  }
  async function boot() {
    try {
      var catalog = await request('/api/catalog');
      state.kind = catalog.chart;
      state.resultColumns = catalog.result_columns;
      state.inputColumns = catalog.columns;
      state.refundReasons = catalog.refund_reasons || {};
      stageButtons(catalog.stages.slice(0, 5));
      document.querySelector('.rail-note').textContent = catalog.code + ' · ' + catalog.stages[6].name;
      await loadStage('intake');
    } catch (error) { setNotice(error.message); }
    $('run').addEventListener('click', runBatch);
    $('sign').addEventListener('click', function () { review('APPROVED'); });
    $('hold').addEventListener('click', function () { review('HELD'); });
    $('refund-form').addEventListener('submit', submitRefund);
    $('profile').addEventListener('change', function () {
      state.run = null; state.approved = false;
      $('sign').disabled = true; $('hold').disabled = true; $('download').hidden = true;
      $('run-status').textContent = '未执行批次'; $('revision').textContent = 'rev 0';
      loadStage('intake');
    });
  }
  document.addEventListener('DOMContentLoaded', boot);
})();
