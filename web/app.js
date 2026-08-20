// Shared behavior for both index_bike.html and index_shade.html. Each page
// sets window.APP_CONFIG before loading this file (see the inline <script>
// in each HTML file) to supply the bits that actually differ: legend
// thresholds/colors, which feature property drives coloring, how the block
// layer is fetched (bike's is a static one-time file; shade's is a live,
// time-of-day-aware backend endpoint with client-side caching), and how to
// describe a clicked block's properties.
const config = window.APP_CONFIG;

function colorFor(value, stops) {
  for (const stop of stops) {
    if (value <= stop.max) return stop.color;
  }
  return stops[stops.length - 1].color;
}

const legend = document.getElementById('legend');
for (const stop of config.legendStops) {
  const row = document.createElement('div');
  row.className = 'legend-row';
  row.innerHTML = `<span class="legend-swatch" style="background:${stop.color}"></span>${stop.label}`;
  legend.appendChild(row);
}

const map = L.map('map', { zoomControl: false }).setView([40.785, -73.977], 14);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution: '&copy; OpenStreetMap, &copy; CartoDB',
  maxZoom: 20,
}).addTo(map);
L.control.zoom({ position: 'bottomright' }).addTo(map);

// --- block layer + click-for-detail ------------------------------------------
const detail = document.getElementById('detail');

function showDetail(props) {
  const rows = config.detailFields(props);
  detail.innerHTML = '<table class="w-full">' + rows.map(([k, v]) =>
    `<tr><td class="text-gray-500 pr-3 py-1 align-top w-2/5">${k}</td><td class="py-1 align-top text-gray-200">${v}</td></tr>`
  ).join('') + '</table>';
}

function renderBlocks(geojson) {
  L.geoJSON(geojson, {
    style: feature => ({
      color: colorFor(feature.properties[config.scoreProp], config.legendStops),
      weight: 4,
      opacity: config.blockLayerOpacity,
    }),
    onEachFeature: (feature, layer) => {
      layer.on('click', () => showDetail(feature.properties));
    },
  }).addTo(map);
}

if (config.blocksMode === 'static') {
  fetch(config.blocksUrl)
    .then(r => r.json())
    .then(renderBlocks)
    .catch(err => {
      detail.innerHTML = `<p class="text-gray-500">Could not load ${config.blocksUrl} -- ${config.blocksErrorHint}. (${err})</p>`;
    });
} else if (config.blocksMode === 'live') {
  const MONTH_NAMES = [
    '', 'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
  ];

  function describeSnapshot(month, hour) {
    const hour12 = hour % 12 || 12;
    const ampm = hour < 12 ? 'AM' : 'PM';
    return `${MONTH_NAMES[month]} · ${hour12}:00 ${ampm}`;
  }

  // Keyed on the visitor's real current (month, hour) -- the shade data
  // itself only varies at that granularity (see scripts/02: fixed day-15
  // snapshots), so any page load within the same hour reuses this instead
  // of re-hitting the backend.
  const CACHE_KEY = 'shadeBlocksCache';

  function loadCachedBlocks(realMonth, realHour) {
    let cached;
    try {
      cached = JSON.parse(localStorage.getItem(CACHE_KEY));
    } catch (err) {
      return null; // corrupt cache entry -- ignore and refetch
    }
    if (cached && cached.realMonth === realMonth && cached.realHour === realHour) return cached;
    return null;
  }

  function cacheBlocks(entry) {
    try {
      localStorage.setItem(CACHE_KEY, JSON.stringify(entry));
    } catch (err) {
      // storage full/unavailable -- non-fatal, just skip caching
    }
  }

  const timeMarker = document.getElementById('time-marker');
  const now = new Date();
  const realMonth = now.getMonth() + 1;
  const realHour = now.getHours();

  const cached = loadCachedBlocks(realMonth, realHour);
  if (cached) {
    if (timeMarker) timeMarker.textContent = describeSnapshot(cached.resolvedMonth, cached.resolvedHour);
    renderBlocks(cached.geojson);
  } else {
    fetch(`${config.blocksEndpointBase}?month=${realMonth}&hour=${realHour}`)
      .then(r => r.json())
      .then(data => {
        if (timeMarker) timeMarker.textContent = describeSnapshot(data.month, data.hour);
        renderBlocks(data.geojson);
        cacheBlocks({
          realMonth, realHour,
          resolvedMonth: data.month, resolvedHour: data.hour,
          geojson: data.geojson,
        });
      })
      .catch(() => {
        // No live backend reachable (e.g. this is a static hosted demo) --
        // fall back to a fixed representative snapshot rather than a bare
        // network error.
        if (timeMarker) timeMarker.textContent = `${config.fallbackLabel} (sample -- live backend not running)`;
        fetch(config.fallbackBlocksUrl)
          .then(r => r.json())
          .then(renderBlocks)
          .catch(err => {
            detail.innerHTML = `<p class="text-gray-500">Could not load shade data. (${err})</p>`;
          });
      });
  }
}

// --- route search form --------------------------------------------------------
// Separate layer group so a computed route never mixes into the
// block-coloring layer above -- distinct style, own group.
const routeLayer = L.layerGroup().addTo(map);
const routeForm = document.getElementById('route-form');
const routeQueryInput = document.getElementById('route-query');
const routeStatus = document.getElementById('route-status');
const routeDirections = document.getElementById('route-directions');
const downloadPdfBtn = document.getElementById('download-pdf-btn');
const routePanel = document.getElementById('route-panel');
const routeSummary = document.getElementById('route-summary');
const distVal = document.getElementById('dist-val');
const timeVal = document.getElementById('time-val');
let lastRoute = null; // { query, summary, directions } -- feeds the PDF download

function autoResizeQuery() {
  routeQueryInput.style.height = 'auto';
  const newHeight = Math.min(routeQueryInput.scrollHeight, 160);
  routeQueryInput.style.height = `${newHeight}px`;
  routeQueryInput.style.overflowY = routeQueryInput.scrollHeight > 160 ? 'auto' : 'hidden';
}
routeQueryInput.addEventListener('input', autoResizeQuery);
routeQueryInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    routeForm.requestSubmit();
  }
});

const STEP_LABELS = {
  llm_parse: 'Understanding query',
  geocode_origin: 'Finding origin',
  geocode_destination: 'Finding destination',
  missing_origin: 'Missing origin',
  unsupported_preference: 'Unsupported preference',
  routing: 'Computing route',
};

function impliesCurrentLocation(query) {
  return /\bmy (current )?location\b|\bwhere i am\b|\bmy position\b|\bfrom here\b/i.test(query);
}

function getCurrentPosition() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) {
      reject(new Error('Geolocation is not supported by this browser.'));
      return;
    }
    navigator.geolocation.getCurrentPosition(
      pos => resolve([pos.coords.latitude, pos.coords.longitude]),
      err => reject(new Error(err.message)),
      { timeout: 10000 },
    );
  });
}

function showRoutePanel() {
  routePanel.classList.remove('panel-hidden');
}

function setRouteStatus(message, kind) {
  routeStatus.textContent = message;
  routeStatus.className = 'px-3 pb-1 text-xs ' + (kind === 'error' ? 'text-red-400' : kind === 'success' ? 'text-green-400' : 'text-gray-400');
  routeSummary.textContent = message;
  routeSummary.className = 'text-sm mb-4 ' + (kind === 'error' ? 'text-red-400' : kind === 'success' ? 'text-gray-300' : 'text-gray-400');
}

function formatDistance(meters) {
  const miles = meters * 0.000621371;
  if (miles < 0.1) return `${Math.round(meters * 3.28084)} ft`;
  return `${miles.toFixed(1)} mi`;
}

function formatTime(seconds) {
  const mins = Math.round(seconds / 60);
  if (mins < 60) return `${mins} min`;
  const hrs = Math.floor(mins / 60);
  const rem = mins % 60;
  return rem ? `${hrs}h ${rem}m` : `${hrs}h`;
}

function renderDirections(directions) {
  routeDirections.innerHTML = '';
  for (const step of directions || []) {
    const li = document.createElement('li');
    li.textContent = step.distance_m > 0 ? `${step.text} (${formatDistance(step.distance_m)})` : step.text;
    routeDirections.appendChild(li);
  }
}

downloadPdfBtn.addEventListener('click', () => {
  if (!lastRoute) return;
  const { jsPDF } = window.jspdf;
  const doc = new jsPDF();
  const marginX = 14;
  const pageBottom = 280;
  let y = 18;

  doc.setFontSize(16);
  doc.text('NYC Route Directions', marginX, y);
  y += 9;

  doc.setFontSize(10);
  doc.setTextColor(90);
  for (const line of doc.splitTextToSize(`Query: ${lastRoute.query}`, 180)) {
    doc.text(line, marginX, y);
    y += 5;
  }
  y += 3;

  doc.setFontSize(11);
  doc.setTextColor(26, 122, 60);
  for (const line of doc.splitTextToSize(lastRoute.summary, 180)) {
    doc.text(line, marginX, y);
    y += 6;
  }
  y += 4;

  doc.setFontSize(11);
  doc.setTextColor(20);
  (lastRoute.directions || []).forEach((step, i) => {
    const label = step.distance_m > 0 ? `${i + 1}. ${step.text} (${formatDistance(step.distance_m)})` : `${i + 1}. ${step.text}`;
    const lines = doc.splitTextToSize(label, 180);
    if (y + lines.length * 6 > pageBottom) {
      doc.addPage();
      y = 18;
    }
    for (const line of lines) {
      doc.text(line, marginX, y);
      y += 6;
    }
  });

  doc.save('nyc-route-directions.pdf');
});

routeForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const query = routeQueryInput.value.trim();
  if (!query) return;

  const submitButton = routeForm.querySelector('button');
  submitButton.disabled = true;
  routeLayer.clearLayers();
  renderDirections([]);
  lastRoute = null;
  downloadPdfBtn.disabled = true;
  distVal.textContent = '--';
  timeVal.textContent = '--';
  showRoutePanel();
  setRouteStatus('Working on it...', '');

  let originCoords = null;
  if (impliesCurrentLocation(query)) {
    try {
      originCoords = await getCurrentPosition();
    } catch (err) {
      setRouteStatus(`Could not get your location: ${err.message}`, 'error');
      submitButton.disabled = false;
      return;
    }
  }

  try {
    const res = await fetch('http://localhost:8000/api/route-query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, origin_coords: originCoords }),
    });
    const data = await res.json();
    if (!res.ok) {
      const label = STEP_LABELS[data.step] || data.step || 'Request failed';
      setRouteStatus(`${label}: ${data.message || 'unknown error'}`, 'error');
      return;
    }
    const geoLayer = L.geoJSON(data.geometry, {
      style: { color: '#ec4899', weight: 5, opacity: 0.95, dashArray: '1, 8' },
    }).addTo(routeLayer);
    map.fitBounds(geoLayer.getBounds(), { padding: [80, 80] });
    setRouteStatus(data.summary, 'success');
    distVal.textContent = formatDistance(data.distance_m);
    timeVal.textContent = formatTime(data.time_s);
    renderDirections(data.directions);
    lastRoute = { query, summary: data.summary, directions: data.directions };
    downloadPdfBtn.disabled = false;
  } catch (err) {
    // Almost always means no backend is reachable at localhost:8000 -- either
    // it's not running locally, or (e.g. on a hosted static demo) there's no
    // backend at all. Either way, a routing-specific message beats a raw
    // fetch error.
    setRouteStatus('Live routing needs the backend running locally (see the project README) -- this hosted demo only shows the map data.', 'error');
  } finally {
    submitButton.disabled = false;
  }
});
