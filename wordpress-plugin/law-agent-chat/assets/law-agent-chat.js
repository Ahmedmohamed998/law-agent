/**
 * Law Agent chat client.
 *
 * Ported from the project's demo/chat.html, which already spoke this contract
 * correctly, with the three things a demo could skip added back:
 *
 *   1. Real tokens. The demo sent `Bearer <user id>`, which only works while
 *      DEV_ALLOW_UNVERIFIED_TOKENS is on. Here a visitor gets an anonymous
 *      token from the product backend and it is refreshed before it expires.
 *   2. The signup gate. POST /consultations is registered-users-only, so a
 *      refused answer has to collect an account before it can collect money.
 *      Signup upgrades the anonymous user row in place, which is why the
 *      conversation survives it.
 *   3. `refused`, not `escalate`. The demo tested source_label === "escalate",
 *      a value the service never emits, so its CTA could never appear.
 *
 * Everything here talks to the two services directly. Nothing is proxied
 * through WordPress: an 8-17 second text/event-stream through PHP arrives as
 * one lump at the end.
 */
(function () {
	'use strict';

	var CFG = window.LawAgentChatConfig || {};

	/**
	 * Every visible label comes from PHP so it can be translated. When that
	 * config fails to print, the widget used to render with blank buttons and
	 * an unlabelled composer -- which looks broken rather than untranslated.
	 * These are the same Arabic strings, as a floor.
	 */
	var DEFAULTS = {
		placeholder: 'اكتب سؤالك…',
		send: 'إرسال',
		newChat: 'محادثة جديدة',
		thinking: 'جارٍ البحث في المستندات…',
		sources: 'المصادر',
		ctaTitle: 'لم نتمكن من الإجابة على هذا السؤال',
		ctaBody: 'يمكنك حجز استشارة مدفوعة مع محامٍ لمتابعة حالتك.',
		ctaButton: 'حجز استشارة مدفوعة — %s',
		ctaButtonPlain: 'حجز استشارة مدفوعة',
		signupTitle: 'أنشئ حسابًا لإتمام الحجز',
		signupBody: 'محادثتك الحالية ستبقى محفوظة في حسابك.',
		signupName: 'الاسم',
		signupEmail: 'البريد الإلكتروني',
		signupPassword: 'كلمة المرور (١٠ أحرف على الأقل)',
		signupSubmit: 'متابعة إلى الدفع',
		signupCancel: 'إلغاء',
		signupWorking: 'جارٍ التحويل إلى صفحة الدفع…',
		loginTitle: 'سجّل الدخول لإتمام الحجز',
		loginBody: 'محادثتك الحالية ستبقى محفوظة وتُربط بحسابك بعد تسجيل الدخول.',
		loginButton: 'تسجيل الدخول',
		registerButton: 'إنشاء حساب جديد',
		labelMixed: 'يتضمن معلومات من خارج المستندات الرسمية',
		labelModel: 'لا يستند إلى المستندات الرسمية — قد يكون غير محدَّث',
		escalated: 'تم تحويل هذه المحادثة إلى محامٍ.',
		errNotReady: 'الخدمة غير متاحة حاليًا. حاول بعد قليل.',
		errAuth: 'انتهت الجلسة. أعد تحميل الصفحة.',
		errNetwork: 'تعذّر الاتصال بالخدمة. حاول مجددًا.',
		errGeneric: 'حدث خطأ غير متوقع. حاول مجددًا.',
		errEmailTaken: 'هذا البريد الإلكتروني مستخدم بالفعل.',
		errOpenConsult: 'لديك استشارة قيد التنفيذ بالفعل.',
		errUnconfigured: 'لم يتم إعداد المساعد بعد.',
		retry: 'إعادة المحاولة'
	};

	var T = (function () {
		var supplied = CFG.i18n || {};
		var out = {};
		for (var k in DEFAULTS) {
			if (Object.prototype.hasOwnProperty.call(DEFAULTS, k)) {
				out[k] = supplied[k] || DEFAULTS[k];
			}
		}
		for (var j in supplied) {
			if (Object.prototype.hasOwnProperty.call(supplied, j) && !out[j]) {
				out[j] = supplied[j];
			}
		}
		return out;
	})();

	var AI = (CFG.aiUrl || '').replace(/\/+$/, '');
	var BE = (CFG.backendUrl || '').replace(/\/+$/, '');

	var AUTH_KEY = 'lawAgent.auth.v1';
	var SESSION_KEY = 'lawAgent.session.v1';

	/* ── storage ──────────────────────────────────────────────────────────
	 * Wrapped because a private window, or a browser set to block site data,
	 * throws on access rather than returning null.
	 */
	function read(key) {
		try {
			var raw = window.localStorage.getItem(key);
			return raw ? JSON.parse(raw) : null;
		} catch (e) {
			return null;
		}
	}

	function write(key, value) {
		try {
			if (value === null) {
				window.localStorage.removeItem(key);
			} else {
				window.localStorage.setItem(key, JSON.stringify(value));
			}
		} catch (e) {
			/* memory-only for this page view; the widget still works */
		}
	}

	/* ── errors ───────────────────────────────────────────────────────── */

	function ApiError(code, message, status) {
		this.code = code || 'error';
		this.message = message || '';
		this.status = status || 0;
	}
	ApiError.prototype = Object.create(Error.prototype);

	function humanError(err) {
		var code = err && err.code;
		if (code === 'session_escalated') return T.escalated;
		if (code === 'not_ready' || code === 'upstream_unavailable') return T.errNotReady;
		if (code === 'unauthenticated') return T.errAuth;
		if (code === 'email_taken' || err.status === 409) return T.errOpenConsult;
		if (code === 'network') return T.errNetwork;
		return T.errGeneric;
	}

	/** Both services answer with { error: { code, message } } on every status. */
	function request(base, path, options) {
		var opts = options || {};
		return fetch(base + path, opts).then(
			function (res) {
				if (res.status === 204) return null;
				return res.text().then(function (text) {
					var body = null;
					try {
						body = text ? JSON.parse(text) : null;
					} catch (e) {
						body = null;
					}
					if (!res.ok) {
						var env = body && body.error ? body.error : {};
						throw new ApiError(env.code, env.message, res.status);
					}
					return body;
				});
			},
			function () {
				throw new ApiError('network', 'fetch failed', 0);
			}
		);
	}

	function json(body) {
		return { 'Content-Type': 'application/json' };
	}

	/* ── auth ─────────────────────────────────────────────────────────────
	 * One token holder for the whole page. The access token lives ~15 minutes;
	 * the refresh token carries the long session and is the revocable half.
	 */
	var Auth = {
		state: read(AUTH_KEY),
		inflight: null,
		// Once per page load. Asking WordPress on every request would put a
		// same-origin round trip in front of every message.
		wpChecked: false,

		user: function () {
			return this.state && this.state.user ? this.state.user : null;
		},

		isAnonymous: function () {
			var u = this.user();
			return !u || u.is_anonymous !== false;
		},

		store: function (payload) {
			this.state = {
				access_token: payload.access_token,
				refresh_token: payload.refresh_token,
				// 30s of slack, so a token never expires mid-request.
				expires_at: Date.now() + Math.max(0, (payload.expires_in || 900) - 30) * 1000,
				user: payload.user || null
			};
			write(AUTH_KEY, this.state);
			return this.state;
		},

		clear: function () {
			this.state = null;
			write(AUTH_KEY, null);
			// The identity is gone, so its conversation is no longer reachable.
			write(SESSION_KEY, null);
		},

		anonymous: function () {
			return request(BE, '/auth/anonymous', { method: 'POST', headers: json() }).then(
				this.store.bind(this)
			);
		},

		/**
		 * Ask WordPress whether this visitor is signed in, and if so exchange
		 * its assertion for our own token.
		 *
		 * The assertion is minted server-side in PHP -- the shared secret never
		 * reaches this file. Any anonymous token already held is sent along,
		 * so the backend links that same user row and the conversation from
		 * before the login stays theirs.
		 */
		fromWordPress: function () {
			var self = this;
			if (!CFG.sessionEndpoint) return Promise.resolve(null);

			// The nonce is what makes the cookie count. WordPress ignores a
			// login cookie on a REST request without it -- silently, with a
			// 200 and logged_in:false, which reads as "not signed in" rather
			// than "you forgot the nonce".
			var wpHeaders = { 'Accept': 'application/json' };
			if (CFG.restNonce) wpHeaders['X-WP-Nonce'] = CFG.restNonce;

			return fetch(CFG.sessionEndpoint, {
				credentials: 'same-origin',
				headers: wpHeaders
			})
				.then(function (res) { return res.json(); })
				.then(function (wp) {
					if (!wp || !wp.logged_in || !wp.assertion) return null;

					var headers = { 'Content-Type': 'application/json' };
					if (self.state && self.state.access_token && self.isAnonymous()) {
						headers.Authorization = 'Bearer ' + self.state.access_token;
					}
					return request(BE, '/auth/wordpress', {
						method: 'POST',
						headers: headers,
						body: JSON.stringify({ assertion: wp.assertion })
					}).then(self.store.bind(self));
				})
				.catch(function () {
					// WordPress being unreachable must not stop a visitor
					// chatting: fall back to anonymous.
					return null;
				});
		},

		refresh: function () {
			var self = this;
			return request(BE, '/auth/refresh', {
				method: 'POST',
				headers: json(),
				body: JSON.stringify({ refresh_token: this.state.refresh_token })
			}).then(
				this.store.bind(this),
				function () {
					// A dead refresh token is not an error the visitor should
					// see: start a fresh anonymous identity instead.
					self.clear();
					return self.anonymous();
				}
			);
		},

		/** Resolves to a usable access token, minting or refreshing as needed. */
		ensure: function () {
			var self = this;
			if (this.inflight) return this.inflight;

			var next;
			if (!this.state || !this.state.access_token) {
				// WordPress first: a signed-in visitor should never be handed
				// an anonymous identity they then have to be migrated off.
				this.wpChecked = true;
				next = this.fromWordPress().then(function (s) {
					return s || self.anonymous();
				});
			} else if (this.isAnonymous() && !this.wpChecked) {
				// Holding a live anonymous token is NOT proof they are still
				// anonymous. Someone who chatted, went off to log in, and came
				// back arrives here with a token that has minutes left on it —
				// and without this check would stay anonymous until it expired,
				// with the booking button still asking them to sign in.
				this.wpChecked = true;
				next = this.fromWordPress().then(function (s) {
					return s || self.state;
				});
			} else if (Date.now() >= this.state.expires_at) {
				next = this.state.refresh_token ? this.refresh() : this.anonymous();
			} else {
				return Promise.resolve(this.state);
			}

			this.inflight = next.then(
				function (s) {
					self.inflight = null;
					return s;
				},
				function (e) {
					self.inflight = null;
					throw e;
				}
			);
			return this.inflight;
		},

		headers: function () {
			return {
				'Authorization': 'Bearer ' + this.state.access_token,
				'Content-Type': 'application/json'
			};
		},

		/**
		 * Register. Sent WITH the anonymous bearer on purpose: the backend
		 * upgrades that same user row, keeping the id, so the conversations
		 * already stored against it stay the new account's own. There is
		 * nothing to migrate and no endpoint to call.
		 */
		signup: function (email, password, displayName) {
			var headers = { 'Content-Type': 'application/json' };
			if (this.state && this.state.access_token) {
				headers.Authorization = 'Bearer ' + this.state.access_token;
			}
			return request(BE, '/auth/signup', {
				method: 'POST',
				headers: headers,
				body: JSON.stringify({
					email: email,
					password: password,
					display_name: displayName || undefined
				})
			}).then(this.store.bind(this));
		}
	};

	/** An authenticated call to either service, retried once on a 401. */
	function authed(base, path, options) {
		var opts = options || {};
		return Auth.ensure().then(function () {
			opts.headers = Auth.headers();
			return request(base, path, opts).catch(function (err) {
				if (err.code !== 'unauthenticated' && err.status !== 401) throw err;
				return Auth.refresh().then(function () {
					opts.headers = Auth.headers();
					return request(base, path, opts);
				});
			});
		});
	}

	/* ── markdown ─────────────────────────────────────────────────────────
	 * Deliberately small: bold, italic, inline code, bullets and tables are
	 * what the answers actually contain (penalty schedules come back as real
	 * Markdown tables). Escaping happens first, so nothing in a model answer
	 * can inject markup.
	 */
	function esc(s) {
		return String(s).replace(/[&<>"]/g, function (c) {
			return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
		});
	}

	function inline(s) {
		return esc(s)
			.replace(/`([^`]+)`/g, '<code>$1</code>')
			.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
			.replace(/(^|\W)\*([^*\n]+)\*/g, '$1<em>$2</em>');
	}

	function markdown(src) {
		var lines = String(src || '').split('\n');
		var out = [];
		var list = null;
		var para = [];

		function flushPara() {
			if (para.length) {
				out.push('<p>' + inline(para.join(' ')) + '</p>');
				para = [];
			}
		}
		function flushList() {
			if (list) {
				out.push('<ul>' + list + '</ul>');
				list = null;
			}
		}
		function cells(row) {
			return row.trim().replace(/^\||\|$/g, '').split('|').map(function (c) {
				return c.trim();
			});
		}

		for (var i = 0; i < lines.length; i++) {
			var line = lines[i];

			if (/^\s*\|/.test(line) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1] || '')) {
				flushPara();
				flushList();
				var head = cells(line);
				var body = '';
				i += 2;
				for (; i < lines.length && /^\s*\|/.test(lines[i]); i++) {
					body += '<tr>' + cells(lines[i]).map(function (c) {
						return '<td>' + inline(c) + '</td>';
					}).join('') + '</tr>';
				}
				i--;
				out.push(
					'<div class="la-table"><table><thead><tr>' +
					head.map(function (c) { return '<th>' + inline(c) + '</th>'; }).join('') +
					'</tr></thead><tbody>' + body + '</tbody></table></div>'
				);
				continue;
			}

			var bullet = line.match(/^\s*[-*·]\s+(.*)$/);
			if (bullet) {
				flushPara();
				list = (list || '') + '<li>' + inline(bullet[1]) + '</li>';
				continue;
			}

			if (!line.trim()) {
				flushPara();
				flushList();
				continue;
			}
			flushList();
			para.push(line.trim());
		}
		flushPara();
		flushList();
		return out.join('');
	}

	var REASON_LABEL = {
		article_lookup: '🎯 مطابقة رقم المادة',
		hybrid: '⭐ دلالي + لفظي',
		semantic: '🧠 بحث دلالي',
		lexical: '🔤 بحث لفظي',
		regulation_join: '🔗 ربط النظام باللائحة'
	};

	function money(amountMinor, currency) {
		var value = (amountMinor || 0) / 100;
		try {
			return new Intl.NumberFormat('ar', {
				style: 'currency',
				currency: currency || 'SAR',
				maximumFractionDigits: 2
			}).format(value);
		} catch (e) {
			return value.toFixed(2) + ' ' + (currency || '');
		}
	}

	function el(tag, className, text) {
		var node = document.createElement(tag);
		if (className) node.className = className;
		if (text !== undefined) node.textContent = text;
		return node;
	}

	/* ── the widget ───────────────────────────────────────────────────── */

	function Widget(root) {
		this.root = root;
		this.thread = root.querySelector('[data-la-thread]');
		this.log = root.querySelector('[data-la-log]');
		this.form = root.querySelector('[data-la-composer]');
		this.input = root.querySelector('[data-la-input]');
		this.sendBtn = root.querySelector('[data-la-send]');
		this.sessionList = root.querySelector('[data-la-sessions]');
		this.newBtn = root.querySelector('[data-la-new]');
		this.bookBar = root.querySelector('[data-la-bookbar]');
		this.bookBtn = root.querySelector('[data-la-book]');

		this.sessionId = null;
		this.locked = false;
		this.pending = null; // { text, clientMessageId } -- survives a retry

		this.applyStrings();
		this.wire();
		this.boot();
	}

	Widget.prototype.applyStrings = function () {
		this.input.placeholder = T.placeholder;
		this.input.setAttribute('aria-label', T.placeholder);
		this.sendBtn.textContent = T.send;
		if (this.newBtn) this.newBtn.textContent = '+ ' + T.newChat;

		// Revealed only when consultations are actually configured, so the
		// widget never offers something that cannot be bought.
		var consult = CFG.consult || {};
		if (this.bookBar && this.bookBtn && consult.enabled && consult.checkout) {
			this.bookBtn.textContent = T.ctaButtonPlain;
			this.bookBar.hidden = false;
			consultationPrice().then(
				function (price) {
					this.bookBtn.textContent = T.ctaButton.replace(
						'%s',
						money(price.amount_cents, price.currency)
					);
				}.bind(this),
				function () { /* label without the figure is still correct */ }
			);
		}
	};

	Widget.prototype.wire = function () {
		var self = this;

		this.form.addEventListener('submit', function (e) {
			e.preventDefault();
			var text = self.input.value.trim();
			if (!text || self.locked) return;
			self.input.value = '';
			// A new user action gets a new id. A retry of THIS action reuses
			// it -- regenerating on retry is what defeats idempotency and
			// pays for the same answer twice.
			self.pending = { text: text, clientMessageId: uuid() };
			self.send(self.pending);
		});

		this.input.addEventListener('keydown', function (e) {
			if (e.key === 'Enter' && !e.shiftKey) {
				e.preventDefault();
				if (typeof self.form.requestSubmit === 'function') {
					self.form.requestSubmit();
				} else {
					self.form.dispatchEvent(new Event('submit', { cancelable: true }));
				}
			}
		});

		if (this.bookBtn) {
			this.bookBtn.addEventListener('click', function () {
				self.showBookingPanel();
			});
		}

		if (this.newBtn) {
			this.newBtn.addEventListener('click', function () {
				// Created lazily on the first message, so an abandoned chat
				// never becomes a row.
				self.sessionId = null;
				write(SESSION_KEY, null);
				self.thread.innerHTML = '';
				self.setLocked(false);
				self.greet();
				self.loadSessions();
			});
		}
	};

	Widget.prototype.boot = function () {
		var self = this;
		if (!AI || !BE) {
			this.thread.appendChild(el('div', 'la-error', T.errUnconfigured));
			this.setLocked(true);
			return;
		}
		Auth.ensure().then(
			function () {
				var stored = read(SESSION_KEY);
				if (stored && stored.id) {
					return self.open(stored.id).catch(function () {
						write(SESSION_KEY, null);
						self.greet();
					});
				}
				self.greet();
				return self.loadSessions();
			},
			function (err) {
				self.thread.appendChild(el('div', 'la-error', humanError(err)));
				self.setLocked(true);
			}
		);
	};

	Widget.prototype.greet = function () {
		if (!CFG.greeting) return;
		var node = el('div', 'la-msg assistant la-greeting');
		var bubble = el('div', 'la-bubble');
		bubble.appendChild(el('div', 'la-md', CFG.greeting));
		node.appendChild(bubble);
		this.thread.appendChild(node);
	};

	Widget.prototype.setLocked = function (locked) {
		this.locked = locked;
		this.input.disabled = locked;
		this.sendBtn.disabled = locked;
		this.root.classList.toggle('is-locked', locked);
	};

	Widget.prototype.scroll = function () {
		this.log.scrollTop = this.log.scrollHeight;
	};

	Widget.prototype.loadSessions = function () {
		var self = this;
		if (!this.sessionList) return Promise.resolve();

		return authed(AI, '/v1/sessions?limit=30', { method: 'GET' }).then(
			function (rows) {
				self.sessionList.innerHTML = '';
				(rows || []).forEach(function (s) {
					var item = el('button', 'la-session' + (s.session_id === self.sessionId ? ' is-current' : ''));
					item.type = 'button';
					item.appendChild(el('span', 'la-session-title', s.title || '…'));
					if (s.status !== 'active') {
						item.appendChild(el('span', 'la-session-status', s.status));
					}
					item.addEventListener('click', function () {
						self.open(s.session_id);
					});
					self.sessionList.appendChild(item);
				});
			},
			function () { /* the list is a convenience; never block chat on it */ }
		);
	};

	Widget.prototype.open = function (id) {
		var self = this;
		return authed(AI, '/v1/sessions/' + encodeURIComponent(id) + '/messages', { method: 'GET' }).then(
			function (messages) {
				self.sessionId = id;
				write(SESSION_KEY, { id: id });
				self.thread.innerHTML = '';

				// Ordered by seq, never created_at: two messages can land in
				// the same millisecond and a timestamp sort will occasionally
				// render an answer above its own question.
				(messages || []).slice().sort(function (a, b) {
					return a.seq - b.seq;
				}).forEach(function (m) {
					var node = self.renderMessage(m.role, m.content, m.source_label);
					if (m.sources && m.sources.length) self.addSources(node, m.sources);
					if (m.role === 'assistant' && m.source_label === 'refused') {
						self.addConsultationCta(node);
					}
				});

				self.setLocked(false);
				self.scroll();
				return self.loadSessions();
			}
		);
	};

	Widget.prototype.renderMessage = function (role, content, sourceLabel) {
		var node = el('div', 'la-msg ' + role);
		var bubble = el('div', 'la-bubble');
		var md = el('div', 'la-md');

		if (role === 'user') {
			md.textContent = content;
		} else {
			md.innerHTML = markdown(content);
		}
		bubble.appendChild(md);
		node.appendChild(bubble);

		if (role === 'assistant' && sourceLabel) {
			this.addLabel(node, sourceLabel);
		}
		this.thread.appendChild(node);
		this.scroll();
		return node;
	};

	/**
	 * source_label is a structured field, not something to parse out of the
	 * Arabic. `documents` is the normal case and gets no chrome; the other two
	 * informational values get a warning the visitor can actually read.
	 */
	Widget.prototype.addLabel = function (node, label) {
		if (label !== 'mixed' && label !== 'model_knowledge') return;
		var existing = node.querySelector('.la-label');
		if (existing) existing.remove();
		var text = label === 'mixed' ? T.labelMixed : T.labelModel;
		node.querySelector('.la-bubble').appendChild(el('div', 'la-label la-label-' + label, text));
	};

	Widget.prototype.addSources = function (node, sources) {
		var old = node.querySelector('.la-sources');
		if (old) old.remove();
		if (!sources || !sources.length) return;

		var box = el('details', 'la-sources');
		var summary = el('summary', null, (T.sources || '') + ' (' + sources.length + ')');
		box.appendChild(summary);

		var ul = el('ul');
		sources.forEach(function (s) {
			var li = el('li');
			li.appendChild(el('strong', null, s.citation));
			li.appendChild(el('span', 'la-reason', ' · ' + (REASON_LABEL[s.reason] || s.reason)));
			ul.appendChild(li);
		});
		box.appendChild(ul);
		node.querySelector('.la-bubble').appendChild(box);
	};

	/* ── sending ──────────────────────────────────────────────────────── */

	Widget.prototype.send = function (action) {
		var self = this;
		this.setLocked(true);
		this.renderMessage('user', action.text);

		var bubble = this.renderMessage('assistant', '');
		var target = bubble.querySelector('.la-md');
		target.textContent = T.thinking || '';
		bubble.classList.add('is-streaming');

		var state = { raw: '', sources: [], lastPaint: 0 };

		return this.ensureSession()
			.then(function () {
				return Auth.ensure();
			})
			.then(function () {
				return fetch(AI + '/v1/sessions/' + encodeURIComponent(self.sessionId) + '/messages/stream', {
					method: 'POST',
					headers: Auth.headers(),
					body: JSON.stringify({
						content: action.text,
						client_message_id: action.clientMessageId
					})
				});
			})
			.then(function (res) {
				if (!res.ok) {
					// A stream that fails before it starts answers as JSON.
					return res.text().then(function (text) {
						var body = null;
						try { body = JSON.parse(text); } catch (e) { body = null; }
						var env = body && body.error ? body.error : {};
						throw new ApiError(env.code, env.message, res.status);
					});
				}
				return self.consume(res, bubble, target, state);
			})
			.then(function () {
				bubble.classList.remove('is-streaming');
				self.pending = null;
				self.setLocked(false);
				return self.loadSessions();
			})
			.catch(function (err) {
				bubble.classList.remove('is-streaming');
				self.failure(target, state, err);
				// An escalated conversation is over for the model: no retry.
				if (err.code === 'session_escalated') {
					self.setLocked(true);
				} else {
					self.setLocked(false);
					self.addRetry(bubble, action);
				}
			});
	};

	Widget.prototype.ensureSession = function () {
		var self = this;
		if (this.sessionId) return Promise.resolve(this.sessionId);

		return authed(AI, '/v1/sessions', {
			method: 'POST',
			body: JSON.stringify({ lang: 'ar' })
		}).then(function (s) {
			self.sessionId = s.session_id;
			write(SESSION_KEY, { id: s.session_id });
			return s.session_id;
		});
	};

	/** Reads the event stream. Frames are separated by a blank line. */
	Widget.prototype.consume = function (res, bubble, target, state) {
		var self = this;
		var reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
		var buf = '';
		var event = null;

		function pump() {
			return reader.read().then(function (chunk) {
				if (chunk.done) return;
				buf += chunk.value;

				var frames = buf.split('\n\n');
				buf = frames.pop();

				frames.forEach(function (frame) {
					frame.split('\n').forEach(function (line) {
						if (line.indexOf('event: ') === 0) {
							event = line.slice(7).trim();
						} else if (line.indexOf('data: ') === 0) {
							var data;
							try {
								data = JSON.parse(line.slice(6));
							} catch (e) {
								return;
							}
							self.handle(event, data, bubble, target, state);
						}
					});
				});
				return pump();
			});
		}
		return pump();
	};

	Widget.prototype.handle = function (event, data, bubble, target, state) {
		if (event === 'token') {
			// The delta is the new tail only. Append, never replace.
			state.raw += data.delta;
			var now = Date.now();
			// Re-rendering Markdown on every token makes the browser the
			// bottleneck rather than the model.
			if (now - state.lastPaint > 150) {
				state.lastPaint = now;
				target.innerHTML = markdown(state.raw);
				this.scroll();
			}
			return;
		}

		if (event === 'sources') {
			// Retrieval takes under a second and generation about nine, so
			// citations can render while the answer is still typing.
			state.sources = data.sources || [];
			this.addSources(bubble, state.sources);
			this.scroll();
			return;
		}

		if (event === 'done') {
			target.innerHTML = markdown(state.raw);
			this.addSources(bubble, state.sources);
			this.addLabel(bubble, data.source_label);
			if (data.source_label === 'refused') {
				this.addConsultationCta(bubble);
			}
			this.scroll();
			return;
		}

		if (event === 'error') {
			this.failure(target, state, new ApiError(data.code, data.message, 0));
			if (data.code === 'session_escalated') this.setLocked(true);
		}
	};

	Widget.prototype.failure = function (target, state, err) {
		target.innerHTML = state.raw ? markdown(state.raw) : '';
		target.appendChild(el('div', 'la-error', humanError(err)));
	};

	Widget.prototype.addRetry = function (bubble, action) {
		var self = this;
		var btn = el('button', 'la-retry', T.retry || '');
		btn.type = 'button';
		btn.addEventListener('click', function () {
			bubble.remove();
			// Same client_message_id: if the first attempt did reach the
			// service, this returns the stored answer instead of paying for
			// a second one.
			self.send(action);
		});
		bubble.querySelector('.la-bubble').appendChild(btn);
	};

	/* ── the consultation CTA ─────────────────────────────────────────────
	 * `refused` means the corpus did not cover the question. That is the
	 * moment the product has something to sell, and the only moment this
	 * widget touches money.
	 */

	/**
	 * What a consultation costs, as the backend says.
	 *
	 * Asked rather than configured: the amount is set by
	 * CONSULTATION_PRICE_CENTS on the product backend and charged from there,
	 * so quoting a number held anywhere else risks putting one figure on the
	 * button and a different one on the invoice. Cached for the page view.
	 */
	var priceRequest = null;

	function consultationPrice() {
		if (!priceRequest) {
			priceRequest = authed(BE, '/consultations/price', { method: 'GET' });
		}
		return priceRequest;
	}

	/** The offer panel. Shared by the `refused` CTA and the persistent bar. */
	Widget.prototype.buildCtaBox = function (opts) {
		var self = this;
		var settings = opts || {};

		var box = el('div', 'la-cta');
		if (settings.title !== false) {
			box.appendChild(el('div', 'la-cta-title', T.ctaTitle));
		}
		box.appendChild(el('div', 'la-cta-body', T.ctaBody));

		var btn = el('button', 'la-cta-button');
		btn.type = 'button';
		// Labelled without a figure until the backend supplies one, rather
		// than guessing and correcting.
		btn.textContent = T.ctaButtonPlain;
		consultationPrice().then(
			function (price) {
				btn.textContent = T.ctaButton.replace(
					'%s',
					money(price.amount_cents, price.currency)
				);
			},
			function () { /* the button still works; only the figure is missing */ }
		);
		btn.addEventListener('click', function () {
			// Anonymous visitors can ask, but not buy: POST /consultations is
			// registered-only. Collect an account first -- and because signup
			// upgrades this same user in place, the conversation above stays
			// theirs afterwards.
			if (Auth.isAnonymous()) {
				self.showSignup(box);
			} else {
				self.checkout(box);
			}
		});
		box.appendChild(btn);
		return box;
	};

	Widget.prototype.addConsultationCta = function (bubble) {
		var consult = CFG.consult || {};
		if (!consult.enabled || !consult.checkout) return;
		if (bubble.querySelector('.la-cta')) return;

		bubble.querySelector('.la-bubble').appendChild(this.buildCtaBox());
		this.scroll();
	};

	/**
	 * Opened from the persistent bar. Appended to the thread rather than
	 * shown in a modal, so it reads as the next turn in the conversation and
	 * the answer it follows stays visible.
	 */
	Widget.prototype.showBookingPanel = function () {
		var existing = this.thread.querySelector('.la-booking-panel');
		if (existing) {
			existing.scrollIntoView({ block: 'center' });
			return;
		}
		var wrap = el('div', 'la-msg assistant la-booking-panel');
		var bubble = el('div', 'la-bubble');
		bubble.appendChild(this.buildCtaBox({ title: false }));
		wrap.appendChild(bubble);
		this.thread.appendChild(wrap);
		this.scroll();
	};

	/**
	 * WordPress owns accounts now, so this does not collect a password -- it
	 * sends people to the WordPress login, with a redirect back to this page.
	 *
	 * Their conversation is not lost by leaving: it is stored against the
	 * anonymous user row, and on return the backend links that same row to the
	 * WordPress user. Same id, same conversations.
	 */
	Widget.prototype.showSignup = function (box) {
		if (box.querySelector('.la-login')) return;

		var panel = el('div', 'la-login');
		panel.appendChild(el('div', 'la-signup-title', T.loginTitle));
		panel.appendChild(el('div', 'la-signup-body', T.loginBody));

		// Back to this page afterwards, so the conversation they were having
		// is still on screen when they return -- and gets linked to the
		// account they just used.
		function withReturn(url) {
			return url + (url.indexOf('?') === -1 ? '?' : '&') +
				'redirect_to=' + encodeURIComponent(window.location.href);
		}

		var actions = el('div', 'la-login-actions');

		var link = el('a', 'la-cta-button', T.loginButton);
		link.href = withReturn(CFG.loginUrl || '#');
		link.rel = 'nofollow';
		actions.appendChild(link);

		// Sites that split sign-in from sign-up get both. A first-time visitor
		// dropped on a login form has to go hunting for the register link,
		// which is the wrong thing to make someone do mid-purchase.
		if (CFG.registerUrl) {
			var reg = el('a', 'la-login-alt', T.registerButton);
			reg.href = withReturn(CFG.registerUrl);
			reg.rel = 'nofollow';
			actions.appendChild(reg);
		}

		panel.appendChild(actions);

		box.appendChild(panel);
		this.scroll();
	};

	Widget.prototype.checkout = function (box, statusNode) {
		var self = this;
		var consult = CFG.consult || {};
		var status = statusNode || el('div', 'la-signup-status');
		if (!statusNode) box.appendChild(status);
		status.textContent = T.signupWorking || '';

		// No amount and no currency: the backend prices this itself. Sending
		// them is now a 400, which is the point -- a price the buyer can edit
		// is not a price.
		return authed(BE, '/consultations', {
			method: 'POST',
			body: JSON.stringify({
				ai_session_id: self.sessionId || undefined
			})
		}).then(
			function (consultation) {
				if (!consultation || !consultation.payment_key) {
					throw new ApiError('error', 'no payment key returned', 0);
				}
				// The gateway takes it from here. Escalation happens
				// server-side when the webhook confirms the money -- this
				// widget never calls escalate itself.
				window.location.href = consult.checkout + '?payment_token=' +
					encodeURIComponent(consultation.payment_key);
			},
			function (err) {
				status.className = 'la-signup-status is-error';
				status.textContent = humanError(err);
			}
		);
	};

	/* ── util ─────────────────────────────────────────────────────────── */

	function uuid() {
		if (window.crypto && typeof window.crypto.randomUUID === 'function') {
			return window.crypto.randomUUID();
		}
		return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
			var r = (Math.random() * 16) | 0;
			var v = c === 'x' ? r : (r & 0x3) | 0x8;
			return v.toString(16);
		});
	}

	function boot() {
		var nodes = document.querySelectorAll('[data-law-agent-chat]');
		for (var i = 0; i < nodes.length; i++) {
			if (!nodes[i].dataset.laMounted) {
				nodes[i].dataset.laMounted = '1';
				new Widget(nodes[i]);
			}
		}
	}

	/**
	 * Mount whenever a widget appears, however it got there.
	 *
	 * An Elementor popup injects its content long after DOMContentLoaded, and
	 * `elementor/frontend/init` may already have fired by the time this file
	 * runs in the footer — so a handler registered for it never executes and
	 * the widget never mounts. The symptom is brutal rather than subtle: the
	 * composer is a real <form>, so pressing send submits it natively and the
	 * page reloads with nothing to show for it.
	 *
	 * Watching the DOM covers popups, tabs, accordions, AJAX and anything else
	 * without knowing which of them did it. boot() is idempotent, so a noisy
	 * observer is only wasted comparisons.
	 */
	function watch() {
		if (!window.MutationObserver || !document.body) return;

		var queued = false;
		new MutationObserver(function () {
			if (queued) return;
			queued = true;
			// Coalesce a burst of mutations into one pass.
			window.requestAnimationFrame(function () {
				queued = false;
				boot();
			});
		}).observe(document.body, { childList: true, subtree: true });
	}

	function start() {
		boot();
		watch();
	}

	if (document.readyState === 'loading') {
		document.addEventListener('DOMContentLoaded', start);
	} else {
		start();
	}

	// Elementor's own signals, for the cases the observer would only catch a
	// frame later. Registered directly when the framework is already up,
	// because waiting for an event that has already fired waits forever.
	function wireElementor() {
		if (!window.elementorFrontend || !window.elementorFrontend.hooks) return;
		window.elementorFrontend.hooks.addAction(
			'frontend/element_ready/law_agent_chat.default',
			boot
		);
	}

	if (window.elementorFrontend) {
		wireElementor();
	} else if (window.jQuery) {
		window.jQuery(window).on('elementor/frontend/init', wireElementor);
	}

	// A popup's content is only reliably in the DOM once it is shown.
	if (window.jQuery) {
		window.jQuery(document).on('elementor/popup/show', function () {
			boot();
		});
	}
})();
