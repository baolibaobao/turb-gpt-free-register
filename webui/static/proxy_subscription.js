(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let state = null;
  let busy = false;
  function message(text, error = false) {
    $('message').textContent = text;
    $('message').classList.toggle('error', error);
  }
  async function api(action, data = {}) {
    const options = action ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)} : {};
    const response = await fetch('/api/proxy-subscription' + (action ? '/' + action : ''), options);
    if (response.status === 401) {
      location.href = '/login?next=/proxy-subscription';
      throw new Error('请重新登录');
    }
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || '操作失败');
    return result;
  }
  function renderNodes() {
    const previous = $('nodes').value || state.selected;
    const query = $('search').value.trim().toLowerCase();
    const nodes = state.nodes.filter(n => (n.name + ' ' + n.type).toLowerCase().includes(query));
    $('nodes').replaceChildren();
    for (const node of nodes) {
      const option = document.createElement('option');
      option.value = node.id;
      option.textContent = node.name + ' · ' + node.type + (node.id === state.selected ? ' ✓' : '');
      $('nodes').append(option);
    }
    if (!nodes.length) {
      const option = document.createElement('option');
      option.value = '';
      option.textContent = state.nodes.length ? '没有匹配的节点' : '请先导入订阅';
      $('nodes').append(option);
    } else if (nodes.some(n => n.id === previous)) $('nodes').value = previous;
    $('count').textContent = `（${nodes.length} / ${state.nodes.length}）`;
  }
  function buttons() {
    document.querySelectorAll('button').forEach(button => {button.disabled = busy;});
    if (busy || !state) return;
    $('enable').disabled = !state.nodes.length || !state.core_installed;
    $('disable').disabled = !state.enabled && !state.running;
    $('test').disabled = !state.enabled;
    $('select').disabled = !state.nodes.length;
    $('ports').disabled = state.enabled || state.running;
  }
  function render(result) {
    state = result;
    $('status').textContent = state.enabled ? (state.healthy ? '已启用' : '已启用 · 核心待恢复') : '未启用';
    $('status').classList.toggle('on', state.enabled && state.healthy);
    $('current').textContent = state.current_node || state.nodes.find(n => n.id === state.selected)?.name || '尚未选择';
    $('endpoint').textContent = state.endpoint;
    $('coreStatus').textContent = state.core_installed ? '独立 Mihomo 核心已就绪' : '请将 Mihomo 核心放入 run/mihomo/bin/mihomo.exe';
    $('saved').textContent = state.has_subscription ? '订阅已保存在本机。留空可刷新现有订阅；填写新链接可替换。' : '支持 Clash / Mihomo YAML、JSON 以及常见 URI / Base64 订阅；推荐 Clash / Mihomo 格式。';
    $('updated').textContent = state.updated_at ? '上次更新：' + state.updated_at : '尚未导入订阅';
    $('warnings').textContent = state.warnings.join('\n');
    $('downloadMode').value = state.download_mode;
    $('mixedPort').value = state.mixed_port;
    $('controllerPort').value = state.controller_port;
    renderNodes();
    buttons();
  }
  async function run(action, data, success) {
    if (busy) return;
    busy = true;
    buttons();
    message('正在处理，请稍候…');
    try {
      const result = await api(action, data);
      if (action === 'test') {
        message(`出口 IP：${result.ip} · 地区：${result.country || '未知'} · 耗时：${result.elapsed_ms} ms`);
      } else {
        render(result);
        if (action === 'refresh') $('url').value = '';
        message(success || result.error || '状态已更新');
      }
    } catch (error) {message(error.message, true);}
    finally {busy = false; buttons();}
  }
  $('search').addEventListener('input', () => {if (state) renderNodes();});
  $('refresh').onclick = () => run('refresh', {url: $('url').value.trim(), download_mode: $('downloadMode').value}, '订阅已保存，请选择节点后启用项目代理。');
  $('select').onclick = () => run('select', {node_id: $('nodes').value}, '节点选择已保存。');
  $('enable').onclick = () => run('enable', {}, '项目代理已启用，可以测试出口 IP。');
  $('disable').onclick = () => run('disable', {}, '项目代理已停用，后续请求恢复原有代理配置。');
  $('test').onclick = () => run('test');
  $('ports').onclick = () => run('ports', {mixed_port: Number($('mixedPort').value), controller_port: Number($('controllerPort').value)}, '独立端口已保存。');
  $('reload').onclick = () => run('');
  run('');
})();
