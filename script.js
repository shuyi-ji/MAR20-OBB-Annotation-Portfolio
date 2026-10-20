async function renderClassChart() {
  const root = document.querySelector('#class-chart');
  if (!root) return;

  try {
    const response = await fetch('data/class_distribution.csv');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const rows = (await response.text()).trim().split(/\r?\n/).slice(1).map((line) => {
      const [classCode, labelName, , , , selectedImages, selectedInstances] = line.split(',');
      return { classCode, labelName, selectedImages: Number(selectedImages), selectedInstances: Number(selectedInstances) };
    });

    const maximum = Math.max(...rows.map((row) => row.selectedInstances));
    const total = rows.reduce((sum, row) => sum + row.selectedInstances, 0);
    root.replaceChildren(...rows.map((row, index) => {
      const item = document.createElement('div');
      item.className = 'chart-row';
      item.title = `${row.labelName}: ${row.selectedInstances} instances across ${row.selectedImages} selected images`;

      const label = document.createElement('span');
      label.className = 'label';
      label.textContent = row.labelName;

      const track = document.createElement('span');
      track.className = 'track';
      const bar = document.createElement('span');
      bar.className = 'bar';
      bar.style.width = `${(row.selectedInstances / maximum) * 100}%`;
      bar.style.animationDelay = `${index * 25}ms`;
      track.append(bar);

      const value = document.createElement('span');
      value.className = 'value';
      value.textContent = row.selectedInstances.toLocaleString('en-US');
      item.append(label, track, value);
      return item;
    }));
    document.querySelector('#chart-total').textContent = total.toLocaleString('en-US');
  } catch (error) {
    root.innerHTML = '<p class="chart-loading">类别数据加载失败，请直接下载 CSV 查看。</p>';
    console.error('Unable to render class chart:', error);
  }
}

renderClassChart();
