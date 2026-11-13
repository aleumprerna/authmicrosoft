(() => {
  const rows = document.getElementById('tracking-rows');
  const table = document.getElementById('tracking-table');
  const summary = document.getElementById('tracking-summary');
  const update = document.getElementById('tracking-update');
  const empty = document.getElementById('tracking-empty');
  const picker = document.getElementById('reply-account');
  const check = document.getElementById('check-replies');
  const status = document.getElementById('reply-status');
  const dialog = document.getElementById('reply-dialog');
  const dialogContent = document.getElementById('reply-dialog-content');
  let latest = {messages: [], reply_checks: [], summary: {}};
  let replyRequest = null;
  let refreshRequest = null;
  let mailboxInitialized = false;
  let connection = null;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function date(value) {
    if (!value) return '';
    return new Date(value).toLocaleString(undefined, {
      day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'
    });
  }
  function badge(text, tone = '') {
    return element('span', `badge${tone ? ` badge-${tone}` : ''}`, text);
  }
  function detail(parent, text) {
    parent.appendChild(element('span', 'cell-detail', text));
  }
  function cell(row, label) {
    const td = element('td');
    td.dataset.label = label;
    const content = element('div', 'cell-content');
    td.appendChild(content);
    row.appendChild(td);
    return content;
  }
  function notice(text, tone = '') {
    status.textContent = text;
    status.className = `notice ${tone}`;
    status.hidden = false;
  }
  function updateLastCheck() {
    if (!picker) return;
    const last = latest.reply_checks.find(item => String(item.account_id) === picker.value);
    document.getElementById('last-reply-check').textContent = last
      ? `Last checked ${date(last.last_checked_at)}` : 'Check your mailbox for new replies.';
  }

  function render(data) {
    const s = data.summary;
    if (picker && !mailboxInitialized && data.messages.length) {
      const recentSender = String(data.messages[0].account_id);
      if ([...picker.options].some(option => option.value === recentSender)) picker.value = recentSender;
      mailboxInitialized = true;
    }
    for (const [id, value] of Object.entries({sent: s.accepted, opened: s.opened, clicked: s.clicked, replied: s.replied})) {
      document.getElementById(`stat-${id}`).textContent = value;
    }
    summary.textContent = `${s.total} email${s.total === 1 ? '' : 's'} · Most recent first`;
    updateLastCheck();
    if (dialog.open) return; // Preserve the reply reader and its focus while polling.
    const focusedEmail = document.activeElement?.dataset.emailId;
    const checked = new Map(data.reply_checks.map(item => [item.account_id, item.last_checked_at]));
    const fragment = document.createDocumentFragment();
    for (const message of data.messages) {
      const row = element('tr');
      const recipient = cell(row, 'Recipient');
      const person = element('div', 'recipient-content');
      const name = message.recipient_name?.trim();
      const initials = name ? name.split(/\s+/).slice(0, 2).map(part => part[0]).join('') : message.recipient.slice(0, 2);
      const avatar = element('span', 'avatar', initials.toUpperCase());
      avatar.setAttribute('aria-hidden', 'true');
      const identity = element('div', 'recipient-text');
      identity.appendChild(element('span', 'recipient-name', name || message.recipient));
      if (name && name.toLowerCase() !== message.recipient.toLowerCase()) detail(identity, message.recipient);
      person.append(avatar, identity);
      person.title = `Sent from ${message.sender}`;
      recipient.appendChild(person);

      const subject = cell(row, 'Subject');
      subject.appendChild(element('span', 'subject-text', message.subject));
      detail(subject, `${message.sent_at ? 'Sent' : 'Created'} ${date(message.sent_at || message.created_at)}`);

      const sending = cell(row, 'Send status');
      const states = {accepted: ['Sent', 'positive'], failed: ['Failed', 'error'], unknown: ['Not confirmed', 'warning'], pending: ['Sending', '']};
      const [label, tone] = states[message.status] || [message.status, ''];
      sending.appendChild(badge(label, tone));
      sending.title = message.error || 'Microsoft accepted this email for sending. Delivery is not confirmed.';
      if (message.error) detail(sending, message.error);

      const opened = cell(row, 'Email open');
      const offline = connection?.endpoints[message.tracking_base_url]?.state === 'offline';
      const unavailable = offline || !!message.tracking_note;
      opened.appendChild(badge(message.opened ? 'Opened' : unavailable ? 'Unavailable' : (message.open_click_enabled ? 'Not detected' : 'Not enabled'), message.opened ? 'positive' : unavailable ? 'warning' : ''));
      if (message.opened) detail(opened, `${message.open_count} open signal${message.open_count === 1 ? '' : 's'}`);
      if (unavailable) detail(opened, message.tracking_note ? 'Tracking was offline at send time' : 'This email’s tracking link is offline');
      if (message.first_opened_at) opened.title = `First detected ${date(message.first_opened_at)}`;

      const clicked = cell(row, 'Link click');
      clicked.appendChild(badge(message.clicked ? 'Clicked' : unavailable ? 'Unavailable' : (message.open_click_enabled ? 'No clicks' : 'Not enabled'), message.clicked ? 'positive' : unavailable ? 'warning' : ''));
      if (message.clicked) detail(clicked, `${message.click_count} click${message.click_count === 1 ? '' : 's'}`);
      if (unavailable) detail(clicked, message.tracking_note ? 'Original links preserved' : 'This email’s tracking link is offline');

      const replies = cell(row, 'Reply');
      const lastCheck = checked.get(message.account_id);
      const hasChecked = message.reply_match_ready && lastCheck && new Date(lastCheck) >= new Date(message.created_at);
      if (message.replied) {
        replies.appendChild(badge('Replied', 'positive'));
        detail(replies, `${message.reply_count} repl${message.reply_count === 1 ? 'y' : 'ies'}`);
        const view = element('button', 'reply-button', 'View reply');
        view.type = 'button';
        view.dataset.emailId = message.email_id;
        view.setAttribute('aria-label', `View replies from ${name || message.recipient} to ${message.subject}`);
        replies.appendChild(view);
      } else {
        replies.appendChild(badge(hasChecked ? 'No reply yet' : 'Not checked'));
        if (!hasChecked) detail(replies, 'Use Check replies');
        if (lastCheck) replies.title = `Last checked ${date(lastCheck)}`;
      }
      fragment.appendChild(row);
    }
    rows.replaceChildren(fragment);
    table.hidden = !data.messages.length;
    empty.hidden = !!data.messages.length;
    if (focusedEmail) [...rows.querySelectorAll('button')].find(button => button.dataset.emailId === focusedEmail)?.focus({preventScroll: true});
  }

  async function refresh() {
    if (refreshRequest) return refreshRequest;
    refreshRequest = (async () => {
      try {
        const response = await fetch('/api/tracking', {cache: 'no-store'});
        if (!response.ok) throw new Error();
        latest = await response.json();
        render(latest);
        update.hidden = true;
      } catch {
        update.textContent = 'Activity could not refresh. Your existing results are kept; retrying automatically.';
        update.hidden = false;
      }
    })();
    try { await refreshRequest; } finally { refreshRequest = null; }
  }

  async function openReplies(emailId) {
    const message = latest.messages.find(item => item.email_id === emailId);
    if (!message) return;
    replyRequest?.abort();
    const controller = new AbortController();
    replyRequest = controller;
    document.getElementById('reply-dialog-title').textContent = 'Replies to your email';
    document.getElementById('reply-dialog-context').textContent = `${message.subject} · Sent to ${message.recipient}`;
    dialogContent.replaceChildren(element('p', 'muted', 'Loading replies from your Microsoft mailbox...'));
    dialogContent.setAttribute('aria-busy', 'true');
    if (!dialog.open) dialog.showModal();
    try {
      const response = await fetch(`/api/tracking/${encodeURIComponent(emailId)}/replies`, {cache: 'no-store', signal: controller.signal});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Could not load this reply. Try again.');
      if (controller.signal.aborted) return;
      dialogContent.replaceChildren();
      if (!data.replies.length) dialogContent.appendChild(element('p', 'muted', 'No matched replies for this email yet. Use Check replies to update.'));
      for (const reply of data.replies) {
        const card = element('article', 'reply-card');
        if (reply.unavailable) {
          card.appendChild(element('p', 'notice notice-warning', reply.error));
          card.appendChild(element('p', 'reply-meta', `Reply received ${date(reply.received_at)}`));
        } else {
          card.appendChild(element('h3', '', reply.subject));
          card.appendChild(element('p', 'reply-meta', `${reply.sender_name || reply.sender}${reply.sender_name ? ` <${reply.sender}>` : ''} · ${date(reply.received_at)}`));
          card.appendChild(element('div', 'reply-body', reply.body || '(This reply has no text content.)'));
        }
        dialogContent.appendChild(card);
      }
    } catch (error) {
      if (error.name !== 'AbortError') dialogContent.replaceChildren(element('p', 'notice notice-error', error.message));
    } finally {
      if (replyRequest === controller) dialogContent.removeAttribute('aria-busy');
    }
  }
  rows.addEventListener('click', event => {
    const button = event.target.closest('button[data-email-id]');
    if (button) openReplies(button.dataset.emailId);
  });
  document.getElementById('close-replies').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { replyRequest?.abort(); refresh(); });
  dialog.addEventListener('click', event => {
    if (event.target === dialog) {
      const bounds = dialog.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
    }
  });
  picker?.addEventListener('change', () => { status.hidden = true; check.textContent = 'Check replies'; updateLastCheck(); });
  check?.addEventListener('click', async () => {
    check.disabled = true;
    picker.disabled = true;
    check.textContent = 'Checking...';
    notice('Checking this Microsoft mailbox for replies...');
    try {
      const response = await fetch(`/tracking/replies/${encodeURIComponent(picker.value)}`, {method: 'POST'});
      const data = await response.json();
      if (!response.ok) {
        notice(data.error || 'Reply check failed. Please try again.', 'notice-error');
        if (response.status === 403) document.getElementById('reply-setup').open = true;
      } else if (data.complete) {
        const added = data.replies_added || 0;
        const hasTrackedEmails = latest.messages.some(message => String(message.account_id) === picker.value);
        notice(!hasTrackedEmails ? 'No tracked emails from this mailbox yet. Select the sender used for your emails.' : added ? `${added} new repl${added === 1 ? 'y' : 'ies'} found. ${data.total_replies || added} replies found in total.`
          : `Up to date. No new replies. ${data.total_replies || 0} repl${data.total_replies === 1 ? 'y was' : 'ies were'} already found.`, 'success');
        if (data.unmatched_sent) status.textContent += ` ${data.unmatched_sent} email(s) are still waiting for a sent copy.`;
      } else {
        notice(data.message, 'notice-warning');
      }
      check.dataset.continue = response.ok && !data.complete ? 'true' : '';
      await refresh();
    } catch {
      notice('Reply check could not complete. Your previous results are kept. Please try again.', 'notice-error');
    } finally {
      check.disabled = false;
      picker.disabled = false;
      check.textContent = check.dataset.continue ? 'Continue checking' : 'Check replies';
    }
  });
  async function poll() {
    if (!document.hidden) await refresh();
    window.setTimeout(poll, 5000);
  }
  async function pollHealth() {
    if (!document.hidden) {
      const banner = document.getElementById('tracking-health');
      try {
        const response = await fetch('/api/tracking/health', {cache: 'no-store'});
        if (!response.ok) throw new Error();
        connection = await response.json();
        const current = connection.current;
        const online = current.state === 'online';
        banner.className = `notice ${online && !connection.unavailable_emails ? 'success' : 'notice-warning'}`;
        banner.textContent = online
          ? 'Open/click tracking is connected for new emails.'
          : 'Open/click tracking is offline. Microsoft sending and reply checks still work. Restart the tracking gateway and tunnel before testing opens.';
        if (connection.unavailable_emails) banner.textContent += ` ${connection.unavailable_emails} earlier email(s) use an offline tracking address; their links cannot record new activity.`;
        if (online && current.temporary) banner.textContent += ' Local test URL: keep the gateway and tunnel running. Restarting the tunnel changes the address for future emails only.';
        if (latest.messages.length) render(latest);
      } catch {
        banner.className = 'notice notice-warning';
        banner.textContent = 'Could not verify the public tracking connection. Do not assume opens are being recorded.';
      }
    }
    window.setTimeout(pollHealth, 15000);
  }
  poll();
  pollHealth();
})();
