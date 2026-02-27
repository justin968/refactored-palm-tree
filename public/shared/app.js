const App = {
  user: localStorage.getItem('trace_user') || '',

  async boot() {
    const d = await this.api('/api/bootstrap', {}, false);
    this.bootstrap = d;

    if (!this.user && location.pathname !== '/login') {
      location.href = '/login';
      return;
    }

    this.currentUser = d.users.find(u => u.id === this.user) || null;

    if (location.pathname === '/login') {
      const sel = document.getElementById('login-user-select');
      if (sel) sel.innerHTML = d.users.map(u => `<option value="${u.id}">${u.name} (${u.role})</option>`).join('');
      return;
    }

    if (!this.currentUser) {
      localStorage.removeItem('trace_user');
      location.href = '/login';
      return;
    }

    const w = document.getElementById('whoami');
    if (w) w.textContent = `Logged in: ${this.currentUser.name} (${this.currentUser.role})`;

    const logoutBtn = document.getElementById('logout-btn');
    if (logoutBtn) {
      logoutBtn.onclick = () => {
        localStorage.removeItem('trace_user');
        location.href = '/login';
      };
    }

    if (this.currentUser.role !== 'ADMIN') {
      document.querySelectorAll('.admin-only').forEach(el => el.style.display = 'none');
    }
  },

  login(userId) {
    localStorage.setItem('trace_user', userId);
    location.href = '/totes/receive';
  },

  async api(url, opts = {}, withAuth = true) {
    const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
    if (withAuth && this.user) headers['x-user-id'] = this.user;
    const res = await fetch(url, { ...opts, headers });
    return res.json();
  },

  msg(el, data) {
    el.textContent = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
  }
};
