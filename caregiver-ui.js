(() => {
  const element = id => document.getElementById(id);
  const status = element('reviewStatus');
  const filter = element('reviewFilter');

  async function api(path, method = 'GET', body, timeout = 5000) {
    const response = await fetch(path, {
      method,
      cache: 'no-store',
      signal: AbortSignal.timeout(timeout),
      ...(body && { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    });
    if (!response.ok) throw new Error(`Request failed (HTTP ${response.status})`);
    return response.json();
  }

  function action(label, handler) {
    const button = document.createElement('button');
    button.className = 'btn';
    button.textContent = label;
    button.onclick = run(handler);
    return button;
  }

  function run(handler) {
    return async event => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await handler();
      } catch (error) {
        status.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    };
  }

  function evidenceLines(assessment) {
    if (!assessment) return [];
    const lines = Array.isArray(assessment.reasons) ? [...assessment.reasons] : [];
    const evidence = assessment.evidence;
    if (assessment.context) lines.push('Activity context: ' + assessment.context);
    if (typeof assessment.quality_ok === 'boolean') lines.push('Signal quality gate: ' + (assessment.quality_ok ? 'passed' : 'did not pass'));
    if (!evidence) return lines;
    if (evidence.signals?.length) lines.push('Signals: ' + evidence.signals.join(', '));
    if (Number.isFinite(evidence.age_seconds_at_assessment)) {
      lines.push(`Sample age at assessment: ${evidence.age_seconds_at_assessment} s. Limit: ${evidence.freshness_limit_seconds} s.`);
    }
    if (evidence.window) {
      const window = evidence.window;
      lines.push(`Motion: ${window.motion.replaceAll('_', ' ')}. Numeric BPM packets: ${window.bpm_packets}/${window.total_packets}. FIFO uninterrupted: ${window.fifo_uninterrupted ? 'yes' : 'no'}.`);
    }
    if (Number.isInteger(evidence.baseline_windows)) lines.push(`Personal baseline size: ${evidence.baseline_windows} windows.`);
    if (Number.isInteger(assessment.consecutive_windows)) {
      lines.push(`Persistence: ${assessment.consecutive_windows} of ${evidence.required_persistence} consecutive flagged windows required.`);
    }
    lines.push(...(evidence.matched_conditions || []));
    for (const [feature, deviation] of Object.entries(evidence.signed_deviations || {})) {
      lines.push(`${feature}: ${assessment.measurements?.[feature]} vs baseline ${assessment.baseline?.[feature]} (${deviation} robust deviation units).`);
    }
    return lines;
  }

  function card(event) {
    const root = document.createElement('article');
    root.className = 'panel review-card';
    const title = document.createElement('h3');
    title.textContent = event.mode === 'simulation' ? 'Simulated check-in' : event.source === 'ble_connection' ? 'Device connection notice' : 'Live experimental check-in';
    const details = document.createElement('p');
    details.textContent = `${new Date(event.observed).toLocaleString()} | ${event.source} | ${event.acknowledged ? 'Acknowledged' : event.read_at ? 'Read' : 'Unread'}`;
    const reason = document.createElement('p');
    reason.textContent = event.reason;
    const label = document.createElement('label');
    label.textContent = 'Caregiver note';
    const note = document.createElement('textarea');
    note.maxLength = 1000;
    note.value = event.note ?? '';
    label.append(note);
    const url = '/wearables/alerts/' + encodeURIComponent(event.id);
    const controls = document.createElement('div');
    controls.className = 'review-controls caregiver-actions';
    if (!event.read_at && !event.acknowledged) controls.append(action('Mark read', async () => {
      await api(url + '/read', 'POST');
      await refresh();
    }));
    if (!event.acknowledged) controls.append(action('Acknowledge', async () => {
      await api(url + '/acknowledge', 'POST');
      await refresh();
    }));
    controls.append(action('Save note', async () => {
      await api(url + '/note', 'PUT', { note: note.value });
      status.textContent = 'Caregiver note saved.';
    }));
    root.append(title, details, reason, label, controls);
    if (event.mode === 'simulation' && !event.acknowledged) {
      controls.append(action('Simulate family notifications', async () => {
        await api('/family/simulation/' + encodeURIComponent(event.id), 'POST');
        status.textContent = 'SIMULATION ONLY. SMS, voice and backup SMS logged locally.';
      }));
    }
    if (event.mode === 'live' && event.source !== 'ble_connection') {
      const context = document.createElement('ul');
      const button = action('Show assessment and patient context', async () => {
        const result = await api('/family/caregiver-context/' + encodeURIComponent(event.id));
        const lines = evidenceLines(result.assessment);
        lines.push('Self-reported diary entries in the 24 hours before the check-in:');
        if (!result.entries.length) lines.push('No diary entries in that window.');
        for (const entry of result.entries) {
          lines.push(`${new Date(entry.occurred_at).toLocaleString()} | ${entry.kind} | ${entry.title} | ${entry.status}${entry.notes ? ' | ' + entry.notes : ''}`);
        }
        context.replaceChildren(...lines.map(line => {
          const item = document.createElement('li');
          item.textContent = line;
          return item;
        }));
      });
      button.classList.add('caregiver-actions');
      root.append(button, context);
    }
    return root;
  }

  element('notificationDeliveries').addEventListener('toggle', async event => {
    if (!event.currentTarget.open) return;
    const status = element('notificationDeliveriesStatus');
    const list = element('notificationDeliveryEntries');
    status.textContent = 'Loading recent attempts.';
    list.replaceChildren();
    try {
      const rows = await api('/family/deliveries');
      for (const row of rows) {
        const item = document.createElement('li');
        const outcome = row.mode === 'simulation' ? 'Simulation only'
          : row.status === 'accepted' ? 'Provider accepted. Handset delivery unverified.' : row.status;
        item.textContent = `${new Date(row.created).toLocaleString()} | ${row.channel.replaceAll('_', ' ')} | ${outcome}`;
        list.append(item);
      }
      status.textContent = rows.length ? `${rows.length} recent attempts.` : 'No notification attempts recorded.';
    } catch (error) {
      status.textContent = 'Notification attempts unavailable. Close and reopen to retry.';
    }
  });

  async function updateBadge() {
    const data = await api('/wearables/alerts');
    const unread = data.unread_by_mode || {};
    const counts = [];
    if (unread.live) counts.push(`${unread.live} live unread`);
    if (unread.simulation) counts.push(`${unread.simulation} simulation unread`);
    element('unreadCount').textContent = counts.length ? '(' + counts.join(', ') + ')' : '';
    const notices = data.events.filter(event => event.source === 'ble_connection' && !event.acknowledged).length;
    element('notificationSummary').textContent = `${unread.live || 0} unread live ? ${unread.simulation || 0} unread simulation ? ${notices} open device notices in recent history`;
    element('releaseReviewState').textContent = (unread.live || 0) + ' live unread';
    element('releaseReviewDetail').textContent = `${unread.simulation || 0} simulation unread. ${notices} open device notices.`;
    return data.events;
  }

  async function refreshOverview() {
    const [serial, ble, ai, connection] = await Promise.allSettled([
      api('/telemetry'), api('/wearables/ble/live'), api('/wearables/ai/summary'), api('/devices/status')
    ]);
    const packet = serial.value;
    const serialAge = Date.now() - Date.parse(packet?.timestamp);
    const fresh = serialAge >= 0 && serialAge <= 10000;
    const espText = serial.status !== 'fulfilled' ? 'Telemetry unavailable'
      : !fresh ? 'No fresh telemetry' : packet.hr_bpm == null ? 'Fresh telemetry, BPM unavailable'
      : `${packet.hr_bpm} BPM`;
    const band = ble.value;
    const bandText = band?.status === 'receiving' ? `${band.hr_bpm} BPM` : 'No fresh BPM';
    const evidence = ai.value?.esp32?.[0];
    const assessment = evidence?.status?.replaceAll('_', ' ') || 'unavailable';
    const details = [assessment];
    if (evidence?.timestamp) details.push('Observed ' + new Date(evidence.timestamp).toLocaleString());
    const liveBpm = evidence?.measurements?.hr_bpm;
    const baselineBpm = evidence?.baseline?.hr_bpm;
    const current = evidence?.status && !['stale', 'unknown_freshness'].includes(evidence.status);
    if (current && Number.isFinite(liveBpm) && Number.isFinite(baselineBpm)) {
      details.push(`Measured ${liveBpm} BPM. Personal reference ${baselineBpm} BPM.`);
      if (Number.isInteger(evidence.consecutive_windows)) details.push(`Consecutive flagged windows: ${evidence.consecutive_windows}.`);
    }
    element('liveEvidence').textContent = details.join(' | ');
    const reasons = current ? evidenceLines(evidence) : [];
    element('liveEvidenceReasons').replaceChildren(...reasons.map(reason => {
      const item = document.createElement('li');
      item.textContent = reason;
      return item;
    }));
    element('releaseEspState').textContent = serial.status !== 'fulfilled' ? 'Unavailable' : fresh ? 'Fresh serial packet' : 'Waiting for data';
    element('releaseEspDetail').textContent = espText;
    const managed = connection.value;
    const managedFresh = managed?.status === 'connected' && managed.measurement?.status === 'receiving';
    element('releaseBleState').textContent = managedFresh || band?.status === 'receiving' ? 'Fresh BLE reading'
      : managed?.status === 'connected' ? 'Connected, awaiting a packet'
      : managed?.status === 'capabilities_only' ? 'Capabilities discovered'
      : managed?.status === 'connection_error' ? 'Connection issue'
      : ble.status !== 'fulfilled' ? 'Unavailable' : 'Waiting for data';
    element('releaseBleDetail').textContent = (managedFresh ? `${managed.measurement.hr_bpm} BPM` : bandText) +
      '. Use Connect to manage wearables.';
    element('caregiverOverview').textContent = `ESP32 ? ${espText} | Sekyo FLEX ? ${bandText} | ESP32 assessment ? ${assessment}`;
  }

  async function refreshHistoricalEvidence() {
    const output = element('historicalEvidence');
    try {
      const result = await api('/wearables/ai/sekyo/replay', 'GET', undefined, 15000);
      const reference = result.reference;
      if (!reference || !Array.isArray(result.readings)) {
        output.textContent = 'Insufficient historical data for a held-out comparison.';
        return;
      }
      output.textContent = `${reference.training_readings} training readings. Training median ${reference.median_bpm} BPM. ` +
        `${result.deviation_count} experimental flags among ${result.readings.length} readings on held-out day ${reference.held_out_day}. ` +
        'Historical replay has no clinical labels or live notifications.';
    } catch (error) {
      output.textContent = 'Historical evidence unavailable. ' + error.message;
    }
  }

  let refreshId = 0;
  async function refresh() {
    const id = ++refreshId;
    status.textContent = 'Loading check-ins.';
    const events = await updateBadge();
    if (id !== refreshId) return;
    const visible = events.filter(event => filter.value === 'simulation'
      ? event.mode === 'simulation'
      : event.mode === 'live' && (filter.value !== 'unread' || (!event.read_at && !event.acknowledged)));
    element('reviewEvents').replaceChildren(...visible.map(card));
    status.textContent = visible.length ? `${visible.length} check-in(s)` : 'No check-ins in this view.';
  }

  async function refreshBaselines() {
    const output = element('calibrationStatus');
    output.textContent = 'Loading calibration profiles.';
    try {
      const [allProfiles, telemetry] = await Promise.all([api('/baseline'), api('/telemetry')]);
      const profiles = allProfiles.filter(profile => profile.mode === 'live');
      const accepted = telemetry.calibration_windows;
      if (Number.isInteger(accepted) && accepted < 30 &&
          Date.now() - Date.parse(telemetry.timestamp) < 10000) {
        output.textContent = `Collecting resting windows: ${accepted} of 30 accepted.`;
        return;
      }
      output.textContent = profiles.length
        ? profiles.map(profile => `${profile.source} (${profile.context}): ${profile.windows} accepted windows`).join(' | ')
        : 'Calibration required. No live baseline has been saved.';
    } catch (error) {
      output.textContent = error.message;
    }
  }

  const report = error => { status.textContent = error.message; };
  element('refreshReviews').onclick = run(refresh);
  filter.onchange = () => refresh().catch(report);
  element('refreshBaselines').onclick = run(refreshBaselines);
  element('makeDemoReview').onclick = run(async () => {
    const result = await api('/wearables/alerts/demo/run', 'POST');
    element('demoTrajectory').textContent = `Simulation: ${result.calibration.windows} training windows. ` +
      result.trajectory.map(row => `${row.measurements.hr_bpm} BPM (${row.status.replaceAll('_', ' ')})`).join(' ? ');
    filter.value = 'simulation';
    location.hash = 'caregiver';
    await refresh();
  });
  element('resetDemoReviews').onclick = run(async () => {
    if (!window.confirm('Delete all simulated check-ins and their notes?')) return;
    const result = await api('/wearables/alerts/demo', 'DELETE');
    element('demoTrajectory').textContent = '';
    window.dispatchEvent(new Event('sentry-simulation-reset'));
    filter.value = 'simulation';
    location.hash = 'caregiver';
    await refresh();
    status.textContent = `Deleted ${result.deleted} simulated check-in(s).`;
  });

  if (location.pathname === '/caregiver' && !location.hash) location.hash = 'caregiver';
  refresh().catch(report);
  refreshOverview();
  refreshHistoricalEvidence();
  refreshBaselines();
  setInterval(() => {
    if (location.hash === '#calibration') refreshBaselines();
  }, 5000);
  setInterval(() => {
    updateBadge().catch(() => {
      element('notificationSummary').textContent = 'Notification totals unavailable. Refresh to retry.';
      element('releaseReviewState').textContent = 'Unavailable';
      element('releaseReviewDetail').textContent = 'Check the local connection and refresh.';
    });
    if (['#caregiver', '#assessment-evidence'].includes(location.hash)) refreshOverview();
  }, 5000);
})();
