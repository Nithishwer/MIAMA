// Adapted from ABFlow's H.A.W.T.O.W.N console; see LICENSE and NOTICE.
const viewButtons = [...document.querySelectorAll('.view-button')];
const runInput = document.getElementById('run-filter');
const runRows = [...document.querySelectorAll('.run-row')];
const compoundInput = document.getElementById('compound-filter');
const compoundRows = [...document.querySelectorAll('.compound-row')];
let exactFilters = {};

function showView(name) {
  document.querySelectorAll('main > .view').forEach(view => {
    view.hidden = view.id !== 'view-' + name;
  });
  viewButtons.forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.view === name));
  });
}

function filterRuns() {
  const query = runInput.value.trim().toLowerCase();
  runRows.forEach(row => {
    const exactMatch = Object.entries(exactFilters).every(([field, value]) => {
      return !value || row.dataset[field] === value;
    });
    row.hidden = !exactMatch || !row.textContent.toLowerCase().includes(query);
  });
  const visible = runRows.filter(row => !row.hidden).length;
  document.getElementById('run-count').textContent = `${visible} / ${runRows.length} records`;
  document.getElementById('run-empty').hidden = visible > 0;
  const labels = [exactFilters.dataset, exactFilters.complex, exactFilters.kind?.replaceAll('_', ' ')].filter(Boolean);
  document.getElementById('exact-filter').hidden = labels.length === 0;
  document.getElementById('exact-filter-label').textContent = labels.join(' · ');
}

function filterCompounds() {
  const query = compoundInput.value.trim().toLowerCase();
  compoundRows.forEach(row => { row.hidden = !row.dataset.search.includes(query); });
  const visible = compoundRows.filter(row => !row.hidden).length;
  document.getElementById('compound-count').textContent = `${visible} / ${compoundRows.length} compounds`;
  document.getElementById('compound-empty').hidden = visible > 0;
}

viewButtons.forEach(button => {
  button.addEventListener('click', () => showView(button.dataset.view));
});
document.querySelectorAll('.jump[data-dataset]').forEach(button => {
  button.addEventListener('click', () => {
    exactFilters = {
      dataset: button.dataset.dataset,
      complex: button.dataset.complex,
      kind: button.dataset.kind,
    };
    runInput.value = '';
    filterRuns();
    showView('runs');
    runInput.focus();
  });
});
document.getElementById('clear-filters').addEventListener('click', () => {
  exactFilters = {};
  runInput.value = '';
  filterRuns();
  runInput.focus();
});
runInput.addEventListener('input', filterRuns);
compoundInput.addEventListener('input', filterCompounds);

const sortButtons = [...document.querySelectorAll('.sort')];
const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
sortButtons.forEach(button => {
  button.addEventListener('click', () => {
    const column = Number(button.dataset.column);
    const numeric = button.dataset.numeric === 'true';
    const ascending = button.closest('th').getAttribute('aria-sort') !== 'ascending';
    const value = row => numeric ? row.cells[column].dataset.sort : row.cells[column].textContent.trim();
    const sorted = [...runRows].sort((leftRow, rightRow) => {
      const left = value(leftRow), right = value(rightRow);
      // Unknown times and shared repeat numbers stay last in either direction.
      if (left === '' || right === '') return Number(left === '') - Number(right === '');
      const comparison = numeric ? Number(left) - Number(right) : collator.compare(left, right);
      return ascending ? comparison : -comparison;
    });
    const body = document.querySelector('.run-table tbody');
    const emptyRow = document.getElementById('run-empty');
    sorted.forEach(row => body.insertBefore(row, emptyRow));
    sortButtons.forEach(other => {
      const active = other === button;
      other.closest('th').setAttribute('aria-sort', active ? (ascending ? 'ascending' : 'descending') : 'none');
      other.querySelector('span').textContent = active ? (ascending ? '↑' : '↓') : '↕';
    });
  });
});

filterRuns();
filterCompounds();
