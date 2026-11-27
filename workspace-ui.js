(() => {
  const el = id => document.getElementById(id);
  const format = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
  const label = value => String(value).replaceAll('_', ' ');

  async function request(path, options) {
    const response = await fetch(path, { signal: AbortSignal.timeout(15000), ...options });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${response.status}`);
    return data;
  }

  function bind(id, event, output, handler) {
    el(id).addEventListener(event, async input => {
      input.preventDefault();
      const control = input.submitter || (event === 'click' ? input.currentTarget : null);
      if (control) control.disabled = true;
      try {
        await handler();
      } catch (error) {
        el(output).textContent = error.message;
      } finally {
        if (control) control.disabled = false;
      }
    });
  }

  function json(method, value) {
    return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) };
  }

  let groups = [];
  const sourceKey = row => JSON.stringify([row.source, row.series]);

  function drawHistory() {
    const visible = groups.filter(row => sourceKey(row) === el('historySource').value);
    const max = visible.reduce((limit, row) => Math.max(limit, row.maximum), 1);
    const chart = el('historyChart');
    const rows = el('historyRows');
    chart.replaceChildren();
    rows.replaceChildren();
    el('historyDetail').textContent = '';
    for (const row of visible) {
      const bar = document.createElement('button');
      bar.className = 'btn';
      bar.type = 'button';
      bar.setAttribute('aria-label', `${row.period}, median ${format.format(row.median)}`);
      const date = document.createElement('span');
      date.textContent = row.period;
      const meter = document.createElement('meter');
      meter.max = max;
      meter.value = row.median;
      meter.setAttribute('aria-hidden', 'true');
      const number = document.createElement('span');
      number.textContent = format.format(row.median);
      bar.append(date, meter, number);
      bar.onclick = () => {
        el('historyDetail').textContent = `${row.period} · ${row.count} samples · minimum ${format.format(row.minimum)} · maximum ${format.format(row.maximum)} · average ${format.format(row.average)}`;
      };
      chart.append(bar);
      const tr = rows.insertRow();
      for (const field of ['period', 'count', 'minimum', 'maximum', 'median', 'average']) {
        tr.insertCell().textContent = typeof row[field] === 'number' ? format.format(row[field]) : row[field];
      }
    }
  }

  let historyRequest = 0;
  async function showHistory() {
    const run = ++historyRequest;
    const params = new URLSearchParams();
    for (const [key, value] of new FormData(el('historyFilter'))) if (value) params.set(key, value);
    el('historyStatus').textContent = 'Loading historical data.';
    const data = await request('/history/analytics?' + params);
    if (run !== historyRequest) return;
    groups = data.groups;
    const sources = new Map(groups.map(row => [sourceKey(row), `${row.source} · ${label(row.series)}`]));
    const previous = el('historySource').value;
    el('historySource').replaceChildren(...Array.from(sources, ([key, name]) => new Option(name, key)));
    if (sources.has(previous)) el('historySource').value = previous;
    el('historyStatus').textContent = groups.length ? 'Showing imported historical measurements.' : 'No readings match these filters.';
    el('historyCsv').href = '/history/report/csv?' + params;
    el('historyPdf').href = '/history/report/pdf?' + params;
    drawHistory();
  }

  bind('historyFilter', 'submit', 'historyStatus', showHistory);
  bind('historySource', 'change', 'historyStatus', drawHistory);
  bind('historyUpload', 'submit', 'uploadStatus', async () => {
    const file = el('historyFile').files[0];
    if (!file) return;
    const kind = file.name.split('.').at(-1).toLowerCase();
    if (!['csv', 'zip'].includes(kind)) throw new Error('Choose a CSV or Da Halo ZIP file.');
    if (file.size > 20000000) throw new Error('Upload limit is 20 MB.');
    el('uploadStatus').textContent = 'Validating and importing file.';
    const result = await request('/history/import/' + kind, { method: 'POST', body: file });
    el('uploadStatus').textContent = `Imported ${result.inserted}. Skipped ${result.duplicates} duplicates.`;
    await showHistory();
  });

  async function connectionState() {
    const state = await request('/devices/status');
    const details = [label(state.status), state.name];
    if (state.battery_percent != null) details.push(`Battery ${state.battery_percent}%`);
    if (state.last_measurement) details.push('Last packet ' + new Date(state.last_measurement).toLocaleTimeString());
    if (state.status === 'connected' && state.measurement) {
      const sample = state.measurement;
      if (sample.status === 'receiving') {
        details.push(`${sample.hr_bpm} BPM`);
        details.push(sample.sensor_contact === true ? 'Contact detected' : 'Contact unknown');
      } else {
        details.push('Awaiting a fresh BPM reading');
      }
    }
    if (state.detail) details.push(state.detail);
    el('deviceConnectionStatus').textContent = details.filter(Boolean).join(' · ');
  }

  async function showCapabilities() {
    const output = el('adaptiveStrategy');
    try {
      const data = await request('/devices/capabilities');
      el('capBleName').textContent = data.ble.name;
      const signals = Object.keys(data.ble.signals);
      const body = el('capabilityRows');
      body.replaceChildren();
      for (const signal of signals) {
        const row = body.insertRow();
        const heading = document.createElement('th');
        heading.scope = 'row';
        heading.textContent = {hrv: 'HRV', spo2: 'SpO?', rr_intervals: 'RR intervals',
          optical_ir: 'Optical IR', raw_electrode_adc: 'Raw electrode ADC'}[signal] || label(signal);
        row.append(heading);
        for (const source of [data.ble, data.serial, data.historical]) {
          const evidence = source.signals[signal];
          const cell = row.insertCell();
          cell.textContent = label(evidence.state) + ' ? ' + evidence.detail;
        }
      }
      const strategy = data.strategy;
      output.textContent = 'BLE strategy: ' + label(strategy.strategy) + '. ' +
        (strategy.cadence_seconds == null ? 'Cadence unknown.' : `Recent median packet gap: ${strategy.cadence_seconds} s.`) +
        ' ' + strategy.persistence + '.';
    } catch (error) {
      output.textContent = 'Capability evidence unavailable. ' + error.message;
    }
  }

  const architecture = {
    wearables: 'ESP32 sends USB serial samples. Commercial devices can send standard Bluetooth LE heart-rate packets. Da Halo history is imported separately.',
    ingestion: 'FastAPI checks packet time, duplicates, available fields and adapter capabilities. SQLite keeps measurements, diary entries and review state locally.',
    ai: 'Adaptive Intelligence looks up a device and context-specific reference. Motion, quality, freshness and persistence gates determine whether the data support a review.',
    review: 'The caregiver sees the explanation, local device notices and preceding diary context. Acknowledgement resolves pending family escalation.',
    response: 'Consent and provider configuration govern SMS and voice. The local simulation records SMS, voice and backup stages without contacting a provider.'
  };
  const architectureButtons = [...el('architectureFlow').querySelectorAll('button')];
  function selectStage(button) {
    for (const candidate of architectureButtons) candidate.setAttribute('aria-pressed', String(candidate === button));
    el('architectureDetail').textContent = architecture[button.dataset.stage];
  }
  for (const button of architectureButtons) button.onclick = () => selectStage(button);
  selectStage(architectureButtons[0]);

  const judgeSteps = [
    ['Care coordination', 'Start with the practical challenge. A wearable reading or an isolated symptom rarely tells a caregiver what changed.', '#project'],
    ['Wearable ecosystem', 'Explore the ring, armband, screenless band, watch and custom sensor as distinct design concepts.', '#wearableStory'],
    ['Verified compatibility', 'Show the ESP32 serial and Sekyo FLEX interfaces and the evidence-backed signal inventory. Waiting states are valid.', '#connection'],
    ['Adaptive Intelligence', 'Follow the architecture from available signals to a quality-gated assessment.', '#innovation'],
    ['Personal baseline', 'Explain device, context and feature matching. Use the genuine Sekyo holdout for historical context.', '#assessment-evidence'],
    ['Labelled simulated check-in', 'Run one local synthetic case and show the reference and consecutive windows.', '#makeDemoReview'],
    ['Family SMS and voice', 'Log a simulated primary SMS, voice and backup response without contacting a provider.', '#family'],
    ['Caregiver acknowledgement', 'Acknowledge the simulated review and open the notification center.', '#caregiver'],
    ['Historical reports', 'Filter an imported history date range and download the report generated by the local backend.', '#analytics'],
    ['Research and limitations', 'Show the offline gain, lower causal result, BIDMC reference comparison, and limits in Advanced Settings.', '#research']
  ];
  let judgeIndex = 0;
  let judgeEvent = null;
  const actionLabels = {
    5: 'Run simulated case',
    6: 'Simulate SMS, voice and backup',
    7: 'Acknowledge simulated check-in'
  };
  function refreshSimulationReview() {
    el('reviewFilter').value = 'simulation';
    el('refreshReviews').click();
  }
  el('startDemo').onclick = event => {
    event.preventDefault();
    judgeIndex = 0;
    showJudgeStep();
    if (location.hash !== '#demo') location.hash = 'demo';
    el('demo').scrollIntoView();
    requestAnimationFrame(() => el('judgeStepTitle').focus({ preventScroll: true }));
  };
  window.addEventListener('sentry-simulation-reset', () => {
    judgeEvent = null;
    el('judgeCaseStatus').textContent = '';
  });
  bind('judgeAction', 'click', 'judgeCaseStatus', async () => {
    const step = judgeIndex;
    const status = el('judgeCaseStatus');
    status.textContent = 'Processing the local simulation.';
    if (step === 5) {
      const result = await request('/wearables/alerts/demo/run', json('POST', {}));
      if (result.mode !== 'simulation' || !result.id) throw new Error('The simulated case could not be confirmed.');
      judgeEvent = result.id;
      const last = result.trajectory.at(-1);
      status.textContent = `SIMULATION ONLY. ${result.calibration.windows} synthetic reference windows. ` +
        `Final observation ${last.measurements.hr_bpm} BPM vs reference ${last.baseline.hr_bpm} BPM. ` +
        `${last.consecutive_windows} consecutive flagged windows. ` + last.reasons.join(' ');
    } else {
      if (!judgeEvent) throw new Error('Run the simulated case in step 6 first.');
      const id = encodeURIComponent(judgeEvent);
      if (step === 6) {
        const result = await request('/family/simulation/' + id, json('POST', {}));
        if (result.status !== 'simulated') throw new Error('The simulated response could not be confirmed.');
        status.textContent = 'SIMULATION ONLY. Primary SMS, automated voice and backup SMS logged locally. No delivery attempted.';
      } else if (step === 7) {
        await request('/wearables/alerts/' + id + '/acknowledge', json('POST', {}));
        status.textContent = 'SIMULATION ONLY. The caregiver acknowledged the check-in.';
      }
    }
    refreshSimulationReview();
  });
  function showJudgeStep() {
    const [title, description, target] = judgeSteps[judgeIndex];
    el('judgeStepTitle').textContent = `Step ${judgeIndex + 1} of ${judgeSteps.length}. ${title}`;
    el('judgeStepText').textContent = description;
    el('judgeOpen').href = target;
    const action = el('judgeAction');
    action.hidden = !actionLabels[judgeIndex];
    action.textContent = actionLabels[judgeIndex] || '';
    el('judgePrevious').disabled = judgeIndex === 0;
    el('judgeNext').disabled = judgeIndex === judgeSteps.length - 1;
  }
  el('judgePrevious').onclick = () => { judgeIndex--; showJudgeStep(); };
  el('judgeNext').onclick = () => { judgeIndex++; showJudgeStep(); };
  showJudgeStep();
  showCapabilities();
  setInterval(() => { if (location.hash === '#connection') showCapabilities(); }, 6000);

  bind('scanWearables', 'click', 'deviceActionStatus', async () => {
    el('deviceActionStatus').textContent = 'Scanning nearby devices.';
    const scan = el('deviceActionStatus');
    scan.classList.add('scanning');
    let devices;
    try {
      devices = await request('/devices/scan', { method: 'POST' });
    } finally {
      scan.classList.remove('scanning');
    }
    el('wearableChoice').replaceChildren(new Option('Select a wearable', ''), ...devices.map(device =>
      new Option(`${device.name} · ${device.address}${device.advertises_heart_rate ? ' · Heart Rate Service' : ''}`, device.address)));
    el('deviceActionStatus').textContent = devices.length ? `Found ${devices.length} nearby devices.` : 'No devices discovered.';
  });
  bind('connectionForm', 'submit', 'deviceActionStatus', async () => {
    await request('/devices/connect', json('POST', { address: el('wearableChoice').value }));
    el('deviceActionStatus').textContent = 'Connection started.';
    await connectionState();
    await showCapabilities();
  });
  bind('stopWearable', 'click', 'deviceActionStatus', async () => {
    await request('/devices/disconnect', { method: 'POST' });
    el('deviceActionStatus').textContent = 'Connection stopped.';
    await connectionState();
    await showCapabilities();
  });

  const profileFields = el('profileForm').elements;
  function setMotion(enabled) {
    document.documentElement.classList.toggle('reduce-motion', enabled);
    localStorage.setItem('sentry-reduced-motion', enabled ? '1' : '0');
    window.dispatchEvent(new CustomEvent('sentry-motion', { detail: enabled }));
  }

  async function loadProfile() {
    const role = el('profileRole').value;
    el('telegramConsent').hidden = role !== 'patient';
    const profile = await request('/account/profiles/' + role);
    profileFields.display_name.value = profile.display_name;
    profileFields.emergency_contact.value = profile.emergency_contact;
    profileFields.reduced_motion.checked = profile.reduced_motion;
    profileFields.telegram_opt_in.checked = profile.telegram_opt_in;
    setMotion(profile.reduced_motion);
  }
  bind('profileRole', 'change', 'accountStatus', loadProfile);
  bind('profileForm', 'submit', 'accountStatus', async () => {
    const role = el('profileRole').value;
    await request('/account/profiles/' + role, json('PUT', {
      display_name: profileFields.display_name.value,
      emergency_contact: profileFields.emergency_contact.value,
      reduced_motion: profileFields.reduced_motion.checked,
      telegram_opt_in: role === 'patient' && profileFields.telegram_opt_in.checked
    }));
    setMotion(profileFields.reduced_motion.checked);
    el('accountStatus').textContent = 'Profile saved.';
  });

  setMotion(localStorage.getItem('sentry-reduced-motion') === '1');
  request('/account').then(async account => {
    el('accountStatus').textContent = account.authentication_enabled ? `Signed in as ${account.role}.` : 'Shared local access. Optional role accounts are configured in the environment.';
    if (account.authentication_enabled) {
      el('profileRole').value = account.role;
      el('profileRole').disabled = true;
      if (account.role === 'caregiver') {
        el('historyUpload').hidden = true;
        el('uploadStatus').hidden = true;
        el('connectionForm').hidden = true;
      }
      if (account.role === 'patient') {
        document.documentElement.classList.add('patient-access');
        el('makeDemoReview').hidden = true;
        el('resetDemoReviews').hidden = true;
      }
    }
    await loadProfile();
  }).catch(error => { el('accountStatus').textContent = error.message; });
  request('/history/metrics').then(metrics => {
    el('historyMetric').replaceChildren(...metrics.map(metric => new Option(label(metric), metric)));
    el('historyMetric').value = 'heart_rate_bpm';
    return showHistory();
  }).catch(error => { el('historyStatus').textContent = error.message; });
  connectionState().catch(error => { el('deviceConnectionStatus').textContent = error.message; });
  setInterval(() => connectionState().catch(error => { el('deviceConnectionStatus').textContent = error.message; }), 3000);
})();
(() => {
  const root = document.querySelector('body.next-ui');
  if (!root) return;
  const get = id => document.getElementById(id);
  const primary = [...document.querySelectorAll('.navlinks a')];
  const grouped = {
    welcome: 'home', wearableStory: 'home', innovation: 'home', demo: 'home', project: 'home',
    'assessment-evidence': 'telemetry', caregiver: 'family', sekyo: 'analytics',
    advancedSettings: 'settings', research: 'settings', calibration: 'settings', devices: 'settings',
    intelligence: 'settings', legacyConcept: 'settings', viewToggle: 'settings', capabilitiesAdvanced: 'settings'
  };

  function revealTarget(destination) {
    const target = get(destination);
    if (!target) return;
    for (let detail = target.closest('details'); detail; detail = detail.parentElement.closest('details')) detail.open = true;
    for (const link of primary) {
      const active = link.hash.slice(1) === (grouped[destination] || destination);
      link.classList.toggle('active', active);
      if (active) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    }
  }

  document.addEventListener('click', event => {
    const link = event.target.closest('a[href^="#"]');
    if (link) revealTarget(link.hash.slice(1));
  }, true);
  window.addEventListener('hashchange', () => revealTarget(location.hash.slice(1)));
  revealTarget(location.hash.slice(1) || 'home');

  const world = get('wearableWorld');
  const storyWorld = world.cloneNode(true);
  storyWorld.removeAttribute('id');
  storyWorld.setAttribute('aria-hidden', 'true');
  storyWorld.removeAttribute('role');
  const tag = storyWorld.querySelector('.tag-top');
  get('storyStage').append(storyWorld);
  const scenes = [
    'Wearable ecosystem', 'Ring sensor layers', 'Biometric armband', 'Monitoring devices',
    'Signal collection', 'Adaptive Intelligence', 'Simulated phone preview', 'Human connection', 'Begin monitoring'
  ];
  const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  function animateAllowed() {
    return !motion.matches && !document.documentElement.classList.contains('reduce-motion') &&
      !root.classList.contains('paused');
  }
  function moveWorld(element, event) {
    if (!animateAllowed() || event.pointerType === 'touch') return;
    const box = element.getBoundingClientRect();
    element.style.setProperty('--world-ry', (((event.clientX - box.left) / box.width) - 0.5) * 18 + 'deg');
    element.style.setProperty('--world-rx', (0.5 - (event.clientY - box.top) / box.height) * 14 + 'deg');
  }
  for (const element of [world, storyWorld]) {
    element.addEventListener('pointermove', event => moveWorld(element, event));
    element.addEventListener('pointerleave', () => {
      element.style.removeProperty('--world-ry');
      element.style.removeProperty('--world-rx');
    });
  }
  const visibility = new IntersectionObserver(entries => {
    for (const item of entries) item.target.classList.toggle('is-visible', item.isIntersecting);
  });
  visibility.observe(world);
  visibility.observe(storyWorld);
  const chapters = [...document.querySelectorAll('#storyChapters [data-scene]')];
  let lastScene = -1;
  let tick = 0;
  function updateScene() {
    tick = 0;
    let active = 0;
    for (let i = 0; i < chapters.length; i++) {
      if (chapters[i].getBoundingClientRect().top <= window.innerHeight * .62) active = i;
    }
    if (active === lastScene) return;
    lastScene = active;
    storyWorld.dataset.scene = String(active);
    tag.textContent = scenes[active];
    chapters.forEach((chapter, i) => chapter.classList.toggle('story-active', i === active));
    const photo = get('familyStoryImage');
    if (photo) photo.hidden = active !== 7;
  }
  function scheduleScene() {
    if (!tick) tick = requestAnimationFrame(updateScene);
  }
  addEventListener('scroll', scheduleScene, {passive: true});
  addEventListener('resize', scheduleScene, {passive: true});
  scheduleScene();

  const icons = {
    ring: 'M3 12a9 9 0 1 0 18 0 9 9 0 0 0-18 0z M7 12a5 5 0 1 0 10 0 5 5 0 0 0-10 0z',
    band: 'M6 4l12 3v13L6 17z M8 9l8 2',
    watch: 'M9 5V2h6v3m-6 14v3h6v-3M6 5h12v14H6z M8 12h3l2-3 2 5h1',
    board: 'M5 5h14v14H5z M9 9h6v6H9z M2 8h3m-3 8h3m14-8h3m-3 8h3',
    patch: 'M7 4h10l4 4v8l-4 4H7l-4-4V8z M9 12h6m-3-3v6'
  };
  const inventory = [
    ['Smart ring','ring','Illustrative','Optical sensor concept. Compatibility depends on a supported interface.'],
    ['Upper-arm band','band','Illustrative','Fabric band and circular sensor. No live pairing claimed.'],
    ['Biometric sensor','patch','Illustrative','A circular sensor concept for contact-based measurements.'],
    ['Screenless band','band','Tested interface','Sekyo FLEX delivered standard Bluetooth heart-rate packets.'],
    ['Smartwatch','watch','Experimental support','Compatible standard heart-rate services can be discovered.'],
    ['Custom ESP32','board','Tested interface','USB serial, experimental optical BPM, motion and raw sensor fields.'],
    ['Health patch','patch','Future concept','No supported pairing or measurements in this prototype.']
  ];
  for (const [title, icon, badge, description] of inventory) {
    const card = document.createElement('article');
    card.className = 'pair-card';
    const symbol = document.createElement('span');
    symbol.className = 'pair-icon';
    symbol.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="' + icons[icon] + '"/></svg>';
    const heading = document.createElement('h3');
    heading.textContent = title;
    const state = document.createElement('small');
    state.textContent = badge;
    const info = document.createElement('p');
    info.textContent = description;
    card.append(symbol, heading, state, info);
    if (title === 'Screenless band' || title === 'Smartwatch' || title === 'Custom ESP32') {
      const link = document.createElement('a');
      link.className = 'btn';
      link.href = title === 'Custom ESP32' ? '#telemetry' : '#connectionForm';
      link.textContent = title === 'Custom ESP32' ? 'View serial status' : 'Find a device';
      card.append(link);
    }
    get('pairGallery').append(card);
  }

  let index = 0;
  const onboarding = [
    ['Welcome to SENTRY AI', 'Set up local monitoring and decide who can follow up when you need a check-in.', '#innovation', 'How it works'],
    ['Patient or caregiver', 'Use your existing sign-in role. In shared local mode, choose the profile to edit in Settings.', '#profileForm', 'Open role and profile'],
    ['Set up your profile', 'Add a display name and an emergency contact if you want one recorded locally.', '#profileForm', 'Edit profile'],
    ['Connect a wearable', 'Scan for a compatible Bluetooth device or check your existing ESP32 serial data.', '#connection', 'Find a device'],
    ['Choose your family contacts', 'Set caregiver numbers and communication consent before enabling provider delivery.', '#family', 'Open family settings'],
    ['Ready to begin', 'View your real monitoring data or try the labelled local demonstration.', '#telemetry', 'Start monitoring']
  ];
  function onboardingStep() {
    const [title, description, target, action] = onboarding[index];
    get('onboardTitle').textContent = title;
    get('onboardText').textContent = description;
    get('onboardingProgress').value = index + 1;
    get('onboardLink').href = target;
    get('onboardLink').textContent = action;
    get('onboardPrevious').disabled = index === 0;
    get('onboardNext').disabled = index === onboarding.length - 1;
  }
  get('onboardNext').onclick = () => { if (index < onboarding.length - 1) index++; onboardingStep(); };
  get('onboardPrevious').onclick = () => { if (index > 0) index--; onboardingStep(); };
  onboardingStep();
})();
