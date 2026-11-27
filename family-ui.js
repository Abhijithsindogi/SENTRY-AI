(() => {
  const el = id => document.getElementById(id);
  const diary = el('diaryForm');
  const family = el('familyForm');
  let trialOnly = false;
  let testLastFour = '';
  const date = new Date();
  date.setMinutes(date.getMinutes() - date.getTimezoneOffset());
  diary.elements.occurred_at.value = date.toISOString().slice(0, 16);
  diary.elements.kind.onchange = () => {
    el('medicationStatus').hidden = diary.elements.kind.value === 'symptom';
  };

  async function api(path, method = 'GET', value) {
    const response = await fetch(path, {
      method, signal: AbortSignal.timeout(15000),
      ...(value && { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Check the form fields.');
    return data;
  }

  function submit(form, output, callback) {
    form.onsubmit = async event => {
      event.preventDefault();
      const button = event.submitter;
      button.disabled = true;
      try {
        await callback();
      } catch (error) {
        output.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    };
  }

  function list(id, rows, format) {
    el(id).replaceChildren(...rows.map(row => {
      const item = document.createElement('li');
      item.textContent = format(row);
      return item;
    }));
  }

  async function refreshDiary() {
    const dates = new URLSearchParams();
    for (const [key, value] of new FormData(el('diaryFilter'))) if (value) dates.set(key, value);
    const query = dates.size ? '?' + dates : '';
    const rows = await api('/family/diary' + query);
    el('diaryPdf').href = '/family/diary.pdf' + query;
    list('diaryEntries', rows, row => [
      new Date(row.occurred_at).toLocaleString(), row.kind, row.title, row.status, row.notes
    ].filter(Boolean).join(' · '));
    el('diaryStatus').textContent = rows.length ? `${rows.length} recent entries.` : 'No diary entries yet.';
  }

  const directory = el('familyDirectoryForm');
  const directoryStatus = el('familyDirectoryStatus');

  async function refreshDirectory() {
    const members = await api('/family/members');
    el('familyMembers').replaceChildren(...members.map(member => {
      const item = document.createElement('li');
      item.textContent = member.name + ' (' + member.relationship + ') ';
      const remove = document.createElement('button');
      remove.className = 'btn';
      remove.textContent = 'Remove';
      remove.onclick = async () => {
        if (!window.confirm('Remove this directory entry?')) return;
        remove.disabled = true;
        try {
          await api('/family/members/' + encodeURIComponent(member.id), 'DELETE');
          await refreshDirectory();
        } catch (error) {
          directoryStatus.textContent = error.message;
          remove.disabled = false;
        }
      };
      item.append(remove);
      return item;
    }));
    directoryStatus.textContent = members.length ? '' : 'No family members registered.';
  }

  submit(directory, directoryStatus, async () => {
    await api('/family/members', 'POST', Object.fromEntries(new FormData(directory)));
    directory.elements.name.value = '';
    await refreshDirectory();
  });

  async function refreshDeliveries() {
    const rows = await api('/family/deliveries');
    list('familyDeliveries', rows, row => [
      new Date(row.created).toLocaleString(), row.mode === 'simulation' ? 'Simulation only' : row.purpose, row.channel, row.status
    ].join(' · '));
    if (!rows.length) list('familyDeliveries', ['No delivery attempts yet.'], row => row);
    rows.forEach((row, index) => {
      if (!row.receipt) return;
      const item = el('familyDeliveries').children[index];
      const describe = value => ' | HTTP ' + value.http_status +
        (value.sid ? ' | SID ' + value.sid : '') +
        (value.provider_status ? ' | Twilio status ' + value.provider_status : '') +
        (value.error_code ? ' | Twilio error ' + value.error_code : '');
      const detail = document.createElement('span');
      detail.textContent = describe(row.receipt);
      item.append(detail);
      if (!row.receipt.sid) return;
      const refresh = document.createElement('button');
      refresh.type = 'button';
      refresh.className = 'btn';
      refresh.textContent = 'Refresh provider status';
      refresh.onclick = async () => {
        refresh.disabled = true;
        try {
          const id = encodeURIComponent(row.id || row.event_id);
          detail.textContent = describe(await api('/family/provider-status/' + id + '/' + row.channel, 'POST'));
        } catch (error) {
          detail.textContent = ' | ' + error.message;
        } finally {
          refresh.disabled = false;
        }
      };
      item.append(refresh);
    });

  }

  submit(el('diaryFilter'), el('diaryStatus'), refreshDiary);
  submit(diary, el('diaryStatus'), async () => {
    const entry = Object.fromEntries(new FormData(diary));
    if (entry.kind === 'symptom') entry.status = 'recorded';
    entry.occurred_at = new Date(entry.occurred_at).toISOString();
    await api('/family/diary', 'POST', entry);
    diary.elements.title.value = '';
    diary.elements.notes.value = '';
    await refreshDiary();
    el('diaryStatus').textContent = 'Diary entry saved.';
  });

  submit(family, el('familyStatus'), async () => {
    const value = { label: family.elements.label.value, phone: family.elements.phone.value.trim() };
    value.backup_phone = family.elements.backup_phone.value.trim();
    for (const key of ['recipient_consented', 'sms', 'voice', 'staged', 'backup_consented']) value[key] = family.elements[key].checked;
    await api('/family/preferences', 'PUT', value);
    el('familyStatus').textContent = 'Contact preferences saved.';
  });

  api('/account').then(async account => {
    if (account.role === 'caregiver') {
      diary.hidden = true;
      family.hidden = true;
      directory.hidden = true;
      el('familyStatus').textContent = 'The patient manages notification preferences.';
      return;
    }
    await refreshDirectory();
    const result = await api('/family/preferences');
    el('familyManual').hidden = account.role !== 'patient' || !result.provider_configured;
    trialOnly = Boolean(result.trial_only);
    el('twilioPreflight').hidden = !trialOnly || !result.provider_configured || account.role !== 'patient';
    if (trialOnly) el('familyManual').querySelector('[data-family-purpose=routine]').hidden = true;
    for (const [key, value] of Object.entries(result.preferences)) {
      if (typeof value === 'boolean') family.elements[key].checked = value;
      else family.elements[key].value = value;
    }
    el('familyStatus').textContent = result.provider_configured
      ? 'Provider configured. Accepted delivery attempts are listed below.'
      : 'Provider setup is required before SENTRY can send SMS or place a call.';
  }).catch(error => { el('familyStatus').textContent = error.message; });
  el('twilioPreflight').onclick = async () => {
    const button = el('twilioPreflight');
    button.disabled = true;
    try {
      const result = await api('/family/trial-preflight');
      const test = result.recipients.test;
      testLastFour = result.active_trial && test?.listed_verified ? test.last_four : '';
      const descriptions = Object.entries(result.recipients).map(([name, recipient]) =>
        `${name} ending ${recipient.last_four}: registry ${recipient.listed_verified === null ? 'unconfirmed' : recipient.listed_verified ? 'matched' : 'no match'}, SMS ${recipient.sms_trial_country_supported ? 'country check passed' : 'blocked by trial country rule'}, voice ${recipient.voice_console_confirmed ? 'Console confirmed' : 'not confirmed'}`);
      el('familyStatus').textContent = [result.active_trial ? 'Active trial.' : 'Active trial unconfirmed.',
        result.free_units_console_confirmed ? 'Free units confirmed locally.' : 'Free units not confirmed.',
        ...descriptions].join(' ');
      const testSms = el('familyManual').querySelector('[data-family-channel=sms][data-family-purpose=test]');
      const testVoice = el('familyManual').querySelector('[data-family-channel=voice]');
      testSms.disabled = !testLastFour || !test.sms_trial_country_supported || !result.free_units_console_confirmed;
      testVoice.disabled = !testLastFour || !test.voice_console_confirmed || !result.free_units_console_confirmed;
    } catch (error) {
      testLastFour = '';
      el('familyStatus').textContent = error.message;
    } finally {
      button.disabled = false;
    }
  };
  for (const button of el('familyManual').querySelectorAll('button')) {
    button.onclick = async () => {
      if (trialOnly && !testLastFour) {
        el('familyStatus').textContent = 'Run the Twilio trial check before confirming a test.';
        return;
      }
      const target = trialOnly ? 'test number ending ' + testLastFour : 'saved primary contact';
      const action = button.dataset.familyChannel === 'voice' ? 'Place one real test call' : 'Send one real SMS';
      if (!window.confirm(action + ' to the consenting ' + target + '?')) return;
      button.disabled = true;
      try {
        const result = await api('/family/manual-delivery', 'POST', {
          channel: button.dataset.familyChannel, purpose: button.dataset.familyPurpose, confirmed: true
        });
        el('familyStatus').textContent = result.status === 'accepted'
          ? 'Provider accepted the request. Confirm receipt on the phone.'
          : 'Provider acceptance was unavailable. Check your provider console before another attempt.';
        if (result.receipt) {
          el('familyStatus').textContent += ' HTTP ' + result.receipt.http_status +
            (result.receipt.sid ? ' | ' + result.receipt.sid : '') +
            (result.receipt.provider_status ? ' | ' + result.receipt.provider_status : '') +
            (result.receipt.error_code ? ' | Twilio error ' + result.receipt.error_code : '');
        }
        await refreshDeliveries();
      } catch (error) {
        el('familyStatus').textContent = error.message;
      } finally {
        button.disabled = false;
      }
    };
  }
  refreshDiary().catch(error => { el('diaryStatus').textContent = error.message; });
  refreshDeliveries().catch(() => {});
  setInterval(() => {
    if (location.hash === '#family') refreshDeliveries().catch(() => {});
  }, 8000);
})();
