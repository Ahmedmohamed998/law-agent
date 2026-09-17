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
		retry: 'إعادة المحاولة',
		remaining: 'الرسائل المتبقية: %s',
		quotaAnonTitle: 'انتهت رسائلك المجانية',
		quotaAnonBody: 'سجّل دخولك لتحصل على %s رسالة إضافية.',
		quotaUserTitle: 'وصلت إلى الحد الأقصى من الرسائل',
		quotaUserBody: 'لمتابعة حالتك، يمكنك حجز استشارة مع محامٍ.',
		micStart: 'تسجيل صوتي',
		micStop: 'إيقاف التسجيل',
		transcribing: 'جارٍ تحويل الصوت إلى نص…',
		micDenied: 'لم نتمكن من الوصول إلى الميكروفون. اسمح بالوصول من إعدادات المتصفح.',
		noSpeech: 'لم نسمع كلامًا واضحًا. حاول مرة أخرى.',
		speechDown: 'خدمة الصوت غير متاحة حاليًا. اكتب سؤالك بدلًا من ذلك.',
		listen: 'استمع',
		stopListening: 'إيقاف',
		loadingAudio: 'جارٍ التحميل…',
		you: 'أنت',
		assistant: 'المستشار',
		cTitle: 'استشاراتي',
		cEmpty: 'لم تحجز أي استشارة بعد.',
		cLoadError: 'تعذّر تحميل الاستشارات. حاول لاحقًا.',
		cNotFound: 'لم نعثر على هذه الاستشارة.',
		cBack: 'العودة إلى استشاراتي',
		cNumber: 'رقم الاستشارة',
		cDate: 'التاريخ',
		cStatus: 'الحالة',
		cAmount: 'المبلغ',
		cPaidAt: 'تاريخ الدفع',
		cLawyer: 'المحامي',
		cLanguage: 'لغة المحادثة',
		cSummary: 'ملخص الحالة المرسل للمحامي',
		cTranscript: 'المحادثة',
		cDetails: 'التفاصيل',
		cOpenChat: 'افتح المحادثة',
		cNoSession: 'لا توجد محادثة مرتبطة بهذه الاستشارة.',
		cTranscriptError: 'تعذّر تحميل المحادثة.',
		cStatusPending: 'بانتظار الدفع',
		cStatusPaid: 'مدفوعة',
		cStatusCancelled: 'ملغاة',
		cStatusRefunded: 'مستردة',
		cEscalated: 'تم إرسالها للمحامي',
		cEscalating: 'قيد الإرسال للمحامي',
		cNotEscalated: 'لم تُرسل بعد',
		cBook: 'حجز استشارة'
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
		if (code === 'no_speech' || code === 'bad_audio') return T.noSpeech;
		if (code === 'speech_unavailable') return T.speechDown;
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

	/* ── voice recording ──────────────────────────────────────────────────
	 * Records 16 kHz mono 16-bit PCM and wraps it as WAV, in the browser.
	 *
	 * Not MediaRecorder: that produces WebM in Chrome, MP4 in Safari and Ogg
	 * in Firefox, none of which Transcribe streaming takes without the server
	 * running ffmpeg. Raw PCM is the one format every browser can produce
	 * through Web Audio and the service can use as it arrives.
	 */
	function Recorder() {}

	Recorder.supported = function () {
		return !!(
			window.isSecureContext &&
			navigator.mediaDevices &&
			navigator.mediaDevices.getUserMedia &&
			(window.AudioContext || window.webkitAudioContext)
		);
	};

	Recorder.prototype.start = function () {
		var self = this;
		return navigator.mediaDevices.getUserMedia({
			audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }
		}).then(function (stream) {
			var Ctx = window.AudioContext || window.webkitAudioContext;
			self.stream = stream;
			self.ctx = new Ctx();
			self.rate = self.ctx.sampleRate;
			self.chunks = [];
			self.samples = 0;
			self.source = self.ctx.createMediaStreamSource(stream);
			// ScriptProcessor is deprecated but works everywhere, unlike an
			// AudioWorklet, which needs a separate module file served with the
			// right headers from inside somebody else's WordPress theme.
			self.node = self.ctx.createScriptProcessor(4096, 1, 1);
			self.node.onaudioprocess = function (e) {
				var data = e.inputBuffer.getChannelData(0);
				self.chunks.push(new Float32Array(data));
				self.samples += data.length;
			};
			self.source.connect(self.node);
			// Chrome only fires onaudioprocess when the node reaches the
			// destination. Nothing is written to the output, so nothing plays.
			self.node.connect(self.ctx.destination);
		});
	};

	Recorder.prototype.seconds = function () {
		return this.rate ? this.samples / this.rate : 0;
	};

	/** Stop, release the microphone, and return a WAV Blob. */
	Recorder.prototype.stop = function () {
		try { this.source.disconnect(); } catch (e) { /* already gone */ }
		try { this.node.disconnect(); } catch (e) { /* already gone */ }
		if (this.stream) {
			// Releasing the tracks is what turns the browser's recording
			// indicator off. Leaving it lit looks like we are still listening.
			this.stream.getTracks().forEach(function (t) { t.stop(); });
		}
		if (this.ctx && this.ctx.close) this.ctx.close();

		var merged = new Float32Array(this.samples);
		var offset = 0;
		this.chunks.forEach(function (c) { merged.set(c, offset); offset += c.length; });
		return encodeWav(downsample(merged, this.rate, 16000), 16000);
	};

	function downsample(buffer, inRate, outRate) {
		if (outRate >= inRate) return buffer;
		var ratio = inRate / outRate;
		var out = new Float32Array(Math.floor(buffer.length / ratio));
		var pos = 0;
		for (var i = 0; i < out.length; i++) {
			// Average the input samples this output sample covers: a cheap
			// low-pass, enough to keep speech from aliasing.
			var next = Math.floor((i + 1) * ratio);
			var sum = 0;
			var count = 0;
			for (; pos < next && pos < buffer.length; pos++) {
				sum += buffer[pos];
				count++;
			}
			out[i] = count ? sum / count : 0;
		}
		return out;
	}

	function encodeWav(samples, rate) {
		var view = new DataView(new ArrayBuffer(44 + samples.length * 2));
		function text(offset, str) {
			for (var i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
		}
		text(0, 'RIFF');
		view.setUint32(4, 36 + samples.length * 2, true);
		text(8, 'WAVE');
		text(12, 'fmt ');
		view.setUint32(16, 16, true);   // PCM header size
		view.setUint16(20, 1, true);    // PCM
		view.setUint16(22, 1, true);    // mono
		view.setUint32(24, rate, true);
		view.setUint32(28, rate * 2, true);
		view.setUint16(32, 2, true);
		view.setUint16(34, 16, true);
		text(36, 'data');
		view.setUint32(40, samples.length * 2, true);
		for (var i = 0, o = 44; i < samples.length; i++, o += 2) {
			var v = Math.max(-1, Math.min(1, samples[i]));
			view.setInt16(o, v < 0 ? v * 0x8000 : v * 0x7fff, true);
		}
		return new Blob([view], { type: 'audio/wav' });
	}

	function formatSeconds(total) {
		var s = Math.floor(total);
		return Math.floor(s / 60) + ':' + (s % 60 < 10 ? '0' : '') + (s % 60);
	}

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
		this.micBtn = root.querySelector('[data-la-mic]');
		this.usageEl = root.querySelector('[data-la-usage]');

		this.sessionId = null;
		this.locked = false;
		this.pending = null; // { text, clientMessageId } -- survives a retry
		// Allowance. `quotaReached` outlives a single conversation: starting a
		// new chat must not quietly unlock a visitor who has used everything.
		this.usage = null;
		this.quotaReached = false;
		// Voice.
		this.recorder = null;
		this.recordTimer = null;
		this.voiceDraft = false; // the input holds a transcript
		this.audio = null;       // { messageId, el, url, button }
		this.audioUrls = {};

		this.applyStrings();
		this.wire();
		this.boot();
	}

	Widget.prototype.applyStrings = function () {
		this.input.placeholder = T.placeholder;
		this.input.setAttribute('aria-label', T.placeholder);
		this.sendBtn.textContent = T.send;
		if (this.newBtn) this.newBtn.textContent = '+ ' + T.newChat;
		if (this.micBtn) {
			this.micBtn.setAttribute('aria-label', T.micStart);
			this.micBtn.title = T.micStart;
			// Only where it can work: a secure page, a browser with Web
			// Audio, and voice switched on in the plugin settings.
			this.micBtn.hidden = !(CFG.voiceEnabled && Recorder.supported());
		}

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
			self.pending = {
				text: text,
				clientMessageId: uuid(),
				// Spoken, even if the visitor corrected a word before sending:
				// the question still started as speech.
				inputMode: self.voiceDraft ? 'voice' : 'text'
			};
			self.voiceDraft = false;
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

		this.input.addEventListener('input', function () {
			if (!self.input.value.trim()) self.voiceDraft = false;
		});

		if (this.micBtn) {
			this.micBtn.addEventListener('click', function () {
				if (self.recorder) {
					self.stopRecording();
				} else {
					self.startRecording();
				}
			});
		}

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
				// Clearing the thread removed the notice, not the limit.
				if (self.quotaReached) self.showQuotaPanel();
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
				// After Auth.ensure, which may have just swapped an anonymous
				// token for a signed-in one: a visitor back from logging in
				// must see their new allowance, not the exhausted old one.
				self.refreshUsage();
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
		// Every caller that unlocks after a turn would otherwise undo the
		// allowance lock, so it is applied here rather than remembered there.
		var effective = locked || this.quotaReached;
		this.locked = effective;
		this.input.disabled = effective;
		this.sendBtn.disabled = effective;
		if (this.micBtn) this.micBtn.disabled = effective && !this.recorder;
		this.root.classList.toggle('is-locked', effective);
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
					var node = self.renderMessage(m.role, m.content, m.source_label, m.input_mode);
					if (m.sources && m.sources.length) self.addSources(node, m.sources);
					if (m.role === 'assistant' && m.content) self.addListen(node, m.message_id);
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

	Widget.prototype.renderMessage = function (role, content, sourceLabel, inputMode) {
		var node = el('div', 'la-msg ' + role + (inputMode === 'voice' ? ' is-voice' : ''));
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
		var userNode = this.renderMessage('user', action.text, null, action.inputMode);

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
						client_message_id: action.clientMessageId,
						input_mode: action.inputMode || 'text'
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
				if (state.errorCode === 'message_quota_exhausted') {
					return self.onQuotaExhausted(action, userNode, bubble);
				}
				self.pending = null;
				self.setLocked(false);
				self.refreshUsage();
				return self.loadSessions();
			})
			.catch(function (err) {
				bubble.classList.remove('is-streaming');
				if (err.code === 'message_quota_exhausted') {
					return self.onQuotaExhausted(action, userNode, bubble);
				}
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
		if (event === 'meta') {
			state.messageId = data.message_id;
			return;
		}

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
			if (state.raw) this.addListen(bubble, state.messageId);
			this.scroll();
			return;
		}

		if (event === 'error') {
			state.errorCode = data.code;
			// Refused before anything was stored: send() removes the bubbles
			// and explains, so there is no failure to render here.
			if (data.code === 'message_quota_exhausted') return;
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

	/* ── message allowance ────────────────────────────────────────────────
	 * The service enforces the limit; this only shows it. Everything here
	 * is safe to get wrong — at worst the counter is stale — because a
	 * message over the limit is refused by the server whatever the page says.
	 */

	Widget.prototype.refreshUsage = function () {
		var self = this;
		return authed(AI, '/v1/usage', { method: 'GET' }).then(
			function (u) {
				self.usage = u;
				var wasReached = self.quotaReached;
				self.quotaReached = u.limit !== null && u.remaining === 0;
				self.renderUsage();

				if (self.quotaReached) {
					self.setLocked(true);
					self.showQuotaPanel();
				} else if (wasReached) {
					// Back from signing in: the anonymous limit no longer applies.
					var panel = self.thread.querySelector('.la-quota');
					if (panel) panel.remove();
					self.setLocked(false);
				}
				return u;
			},
			function () { /* the counter is a convenience; never block chat on it */ }
		);
	};

	Widget.prototype.renderUsage = function () {
		if (!this.usageEl) return;
		var u = this.usage;
		// Staff are unlimited, and a counter reading "∞" is noise.
		if (!u || u.limit === null) {
			this.usageEl.hidden = true;
			return;
		}
		this.usageEl.hidden = false;
		this.usageEl.textContent = T.remaining.replace('%s', String(u.remaining));
		this.usageEl.classList.toggle('is-low', u.remaining <= 2);
	};

	Widget.prototype.onQuotaExhausted = function (action, userNode, bubble) {
		// Nothing was stored, so nothing stays on screen as if it had been.
		if (userNode) userNode.remove();
		if (bubble) bubble.remove();
		// Their words go back in the box: after signing in they can send them.
		this.input.value = action.text;
		this.voiceDraft = action.inputMode === 'voice';
		this.pending = null;
		this.quotaReached = true;
		this.setLocked(true);
		this.showQuotaPanel();
		return this.refreshUsage();
	};

	Widget.prototype.showQuotaPanel = function () {
		if (this.thread.querySelector('.la-quota')) return;

		var wrap = el('div', 'la-msg assistant la-quota');
		var bubble = el('div', 'la-bubble');
		var anonymous = Auth.isAnonymous();

		if (anonymous) {
			var u = this.usage;
			var extra = u && u.registered_limit ? Math.max(0, u.registered_limit - u.used) : null;
			var box = el('div', 'la-cta');
			box.appendChild(el('div', 'la-cta-title', T.quotaAnonTitle));
			if (extra) {
				box.appendChild(el('div', 'la-cta-body', T.quotaAnonBody.replace('%s', String(extra))));
			}
			bubble.appendChild(box);
			// Same sign-in / register panel the booking flow uses, with the
			// same redirect back to this conversation.
			this.showSignup(box);
		} else {
			var consult = CFG.consult || {};
			var head = el('div', 'la-cta');
			head.appendChild(el('div', 'la-cta-title', T.quotaUserTitle));
			head.appendChild(el('div', 'la-cta-body', T.quotaUserBody));
			bubble.appendChild(head);
			if (consult.enabled && consult.checkout) {
				bubble.appendChild(this.buildCtaBox({ title: false }));
			}
		}

		wrap.appendChild(bubble);
		this.thread.appendChild(wrap);
		this.scroll();
	};

	/* ── speaking ─────────────────────────────────────────────────────── */

	Widget.prototype.setMicState = function (state, seconds) {
		if (!this.micBtn) return;
		this.micBtn.classList.toggle('is-recording', state === 'recording');
		this.micBtn.classList.toggle('is-busy', state === 'busy');
		this.micBtn.disabled = state === 'busy' || (state === 'idle' && this.locked);
		var label = state === 'recording' ? T.micStop : T.micStart;
		this.micBtn.setAttribute('aria-label', label);
		this.micBtn.title = label;
		this.micBtn.textContent = state === 'recording' ? formatSeconds(seconds || 0) : '';

		if (state === 'busy') {
			this.input.placeholder = T.transcribing;
		} else {
			this.input.placeholder = T.placeholder;
		}
	};

	Widget.prototype.startRecording = function () {
		var self = this;
		if (this.locked || this.recorder) return;
		this.clearVoiceError();

		var recorder = new Recorder();
		recorder.start().then(
			function () {
				self.recorder = recorder;
				self.setMicState('recording', 0);
				var max = CFG.voiceMaxSeconds || 60;
				self.recordTimer = window.setInterval(function () {
					var secs = recorder.seconds();
					self.setMicState('recording', secs);
					// The server refuses anything longer; stopping here means
					// a long question is sent rather than lost.
					if (secs >= max) self.stopRecording();
				}, 250);
			},
			function () {
				self.showVoiceError(T.micDenied);
			}
		);
	};

	Widget.prototype.stopRecording = function () {
		var self = this;
		if (!this.recorder) return;
		window.clearInterval(this.recordTimer);
		var recorder = this.recorder;
		this.recorder = null;

		if (recorder.seconds() < 0.5) {
			// A tap, not a question.
			recorder.stop();
			this.setMicState('idle');
			return;
		}

		var wav = recorder.stop();
		this.setMicState('busy');

		Auth.ensure()
			.then(function (auth) {
				return request(AI, '/v1/transcribe', {
					method: 'POST',
					headers: {
						'Authorization': 'Bearer ' + auth.access_token,
						'Content-Type': 'audio/wav'
					},
					body: wav
				});
			})
			.then(
				function (result) {
					// Into the box, not straight into the chat: the visitor sees
					// what was heard and can fix a misheard word before a legal
					// question is answered on the strength of it.
					var existing = self.input.value.trim();
					self.input.value = existing ? existing + ' ' + result.text : result.text;
					self.voiceDraft = true;
					self.input.focus();
				},
				function (err) {
					if (err.code === 'message_quota_exhausted') {
						self.quotaReached = true;
						self.setLocked(true);
						self.showQuotaPanel();
						return;
					}
					self.showVoiceError(humanError(err));
				}
			)
			.then(function () {
				self.setMicState('idle');
			});
	};

	Widget.prototype.showVoiceError = function (message) {
		this.clearVoiceError();
		var note = el('div', 'la-voice-error', message);
		this.form.parentNode.insertBefore(note, this.form);
		window.setTimeout(function () { note.remove(); }, 6000);
	};

	Widget.prototype.clearVoiceError = function () {
		var old = this.root.querySelector('.la-voice-error');
		if (old) old.remove();
	};

	/* ── listening ─────────────────────────────────────────────────────── */

	Widget.prototype.addListen = function (node, messageId) {
		var self = this;
		if (!CFG.voiceEnabled || !messageId || node.querySelector('.la-listen')) return;

		var btn = el('button', 'la-listen');
		btn.type = 'button';
		btn.textContent = T.listen;
		btn.addEventListener('click', function () {
			self.toggleListen(messageId, btn);
		});
		node.querySelector('.la-bubble').appendChild(btn);
	};

	Widget.prototype.toggleListen = function (messageId, btn) {
		var self = this;

		// One voice at a time. Pressing the playing answer stops it; pressing
		// another switches to that one.
		if (this.audio) {
			var same = this.audio.messageId === messageId;
			this.stopListening();
			if (same) return;
		}

		function play(url) {
			var audio = new Audio(url);
			self.audio = { messageId: messageId, el: audio, button: btn };
			btn.textContent = T.stopListening;
			btn.classList.add('is-playing');
			audio.addEventListener('ended', function () { self.stopListening(); });
			audio.play().catch(function () { self.stopListening(); });
		}

		if (this.audioUrls[messageId]) {
			play(this.audioUrls[messageId]);
			return;
		}

		btn.disabled = true;
		btn.textContent = T.loadingAudio;
		Auth.ensure()
			.then(function (auth) {
				return fetch(AI + '/v1/messages/' + encodeURIComponent(messageId) + '/audio', {
					headers: { 'Authorization': 'Bearer ' + auth.access_token }
				});
			})
			.then(function (res) {
				if (!res.ok) {
					return res.text().then(function (text) {
						var body = null;
						try { body = JSON.parse(text); } catch (e) { body = null; }
						var env = body && body.error ? body.error : {};
						throw new ApiError(env.code, env.message, res.status);
					});
				}
				return res.blob();
			})
			.then(
				function (blob) {
					btn.disabled = false;
					// Kept for the page view, so a second listen costs nothing.
					self.audioUrls[messageId] = URL.createObjectURL(blob);
					play(self.audioUrls[messageId]);
				},
				function (err) {
					btn.disabled = false;
					btn.textContent = T.listen;
					self.showVoiceError(humanError(err));
				}
			);
	};

	Widget.prototype.stopListening = function () {
		if (!this.audio) return;
		this.audio.el.pause();
		this.audio.button.textContent = T.listen;
		this.audio.button.classList.remove('is-playing');
		this.audio = null;
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

	/* ── the consultations page ───────────────────────────────────────────
	 * "استشاراتي" in My Account. Reads GET /consultations with the visitor's
	 * own token, so it can only ever list theirs -- the backend scopes the
	 * route to the caller, not this page.
	 *
	 * Read-only. Paying happens in the chat; this is where a client comes
	 * back to see what they paid for and whether the lawyer has it.
	 */
	function Consultations(root) {
		this.root = root;
		this.id = root.getAttribute('data-consultation') || '';
		this.root.innerHTML = '';
		this.load();
	}

	Consultations.STATUS = {
		pending: 'cStatusPending',
		paid: 'cStatusPaid',
		cancelled: 'cStatusCancelled',
		refunded: 'cStatusRefunded'
	};

	Consultations.prototype.load = function () {
		var self = this;
		this.root.appendChild(el('div', 'la-account-loading', T.loadingAudio));

		var req = this.id
			? authed(BE, '/consultations/' + encodeURIComponent(this.id), { method: 'GET' })
			: authed(BE, '/consultations', { method: 'GET' });

		req.then(
			function (data) {
				self.root.innerHTML = '';
				if (self.id) {
					self.renderDetail(data);
				} else {
					self.renderList(data || []);
				}
			},
			function (err) {
				self.root.innerHTML = '';
				var msg = err && err.status === 404 ? T.cNotFound : T.cLoadError;
				self.root.appendChild(el('div', 'la-error', msg));
				if (self.id && CFG.accountUrl) self.root.appendChild(self.backLink());
			}
		);
	};

	/** Newest first: the one they just paid for is the one they came to see. */
	Consultations.prototype.renderList = function (rows) {
		var self = this;
		rows = rows.slice().sort(function (a, b) {
			return String(b.created_at).localeCompare(String(a.created_at));
		});

		if (!rows.length) {
			var empty = el('div', 'la-account-empty');
			empty.appendChild(el('p', '', T.cEmpty));
			var consult = CFG.consult || {};
			if (consult.enabled && CFG.chatUrl) {
				var go = el('a', 'la-cta-button', T.cBook);
				go.href = CFG.chatUrl;
				empty.appendChild(go);
			}
			this.root.appendChild(empty);
			return;
		}

		var list = el('div', 'la-clist');
		rows.forEach(function (c) {
			list.appendChild(self.card(c));
		});
		this.root.appendChild(list);
	};

	Consultations.prototype.card = function (c) {
		var card = el('article', 'la-ccard is-' + (c.status || 'pending'));

		var head = el('div', 'la-ccard-head');
		head.appendChild(el('span', 'la-ccard-date', fmtDate(c.created_at)));
		head.appendChild(this.statusBadge(c));
		card.appendChild(head);

		var body = el('div', 'la-ccard-body');
		body.appendChild(el('span', 'la-ccard-amount', money(c.amount_cents, c.currency)));
		body.appendChild(el('span', 'la-ccard-lawyer', this.lawyerText(c)));
		card.appendChild(body);

		var actions = el('div', 'la-ccard-actions');
		var details = el('a', 'la-clink', T.cDetails);
		details.href = this.detailUrl(c.consultation_id);
		actions.appendChild(details);
		if (c.ai_session_id) {
			actions.appendChild(this.openChatLink(c.ai_session_id));
		}
		card.appendChild(actions);

		return card;
	};

	Consultations.prototype.renderDetail = function (c) {
		if (CFG.accountUrl) this.root.appendChild(this.backLink());

		var head = el('div', 'la-cdetail-head');
		head.appendChild(el('h3', 'la-cdetail-title', T.cTitle + ' — ' + shortId(c.consultation_id)));
		head.appendChild(this.statusBadge(c));
		this.root.appendChild(head);

		var dl = el('dl', 'la-cfacts');
		function fact(label, value) {
			if (value === null || value === undefined || value === '') return;
			dl.appendChild(el('dt', '', label));
			dl.appendChild(el('dd', '', value));
		}
		fact(T.cNumber, c.consultation_id);
		fact(T.cDate, fmtDate(c.created_at));
		fact(T.cAmount, money(c.amount_cents, c.currency));
		fact(T.cPaidAt, c.paid_at ? fmtDate(c.paid_at) : null);
		fact(T.cLawyer, this.lawyerText(c));
		fact(T.cLanguage, langName(c.chat_language));
		this.root.appendChild(dl);

		if (c.escalation_summary) {
			var sum = el('section', 'la-csection');
			sum.appendChild(el('h4', '', T.cSummary));
			sum.appendChild(el('p', 'la-csummary', c.escalation_summary));
			this.root.appendChild(sum);
		}

		var tx = el('section', 'la-csection');
		var txHead = el('div', 'la-csection-head');
		txHead.appendChild(el('h4', '', T.cTranscript));
		if (c.ai_session_id) txHead.appendChild(this.openChatLink(c.ai_session_id));
		tx.appendChild(txHead);
		this.root.appendChild(tx);

		if (!c.ai_session_id) {
			tx.appendChild(el('p', 'la-cmuted', T.cNoSession));
			return;
		}

		var thread = el('div', 'la-cthread');
		thread.appendChild(el('div', 'la-account-loading', T.loadingAudio));
		tx.appendChild(thread);

		// The user owns this session, so their own token reads it. Ordered by
		// seq for the same reason the widget does.
		authed(AI, '/v1/sessions/' + encodeURIComponent(c.ai_session_id) + '/messages', { method: 'GET' }).then(
			function (messages) {
				thread.innerHTML = '';
				(messages || []).slice().sort(function (a, b) { return a.seq - b.seq; }).forEach(function (m) {
					var row = el('div', 'la-cmsg ' + m.role + (m.input_mode === 'voice' ? ' is-voice' : ''));
					row.appendChild(el('span', 'la-cmsg-who', m.role === 'user' ? T.you : T.assistant));
					var body = el('div', 'la-cmsg-body la-md');
					body.innerHTML = markdown(m.content || '');
					row.appendChild(body);
					thread.appendChild(row);
				});
				if (!thread.childNodes.length) thread.appendChild(el('p', 'la-cmuted', T.cNoSession));
			},
			function () {
				thread.innerHTML = '';
				thread.appendChild(el('p', 'la-error', T.cTranscriptError));
			}
		);
	};

	Consultations.prototype.statusBadge = function (c) {
		var key = Consultations.STATUS[c.status] || 'cStatusPending';
		return el('span', 'la-cstatus is-' + (c.status || 'pending'), T[key]);
	};

	/** Handover state matters more to a client than the payment row does. */
	Consultations.prototype.lawyerText = function (c) {
		if (c.escalated) return T.cEscalated;
		if (c.status === 'paid') return T.cEscalating;
		return T.cNotEscalated;
	};

	Consultations.prototype.detailUrl = function (id) {
		if (CFG.accountUrl) {
			return CFG.accountUrl.replace(/\/+$/, '') + '/' + encodeURIComponent(id) + '/';
		}
		var here = window.location.href.split('#')[0].replace(/[?&]consultation=[^&]*/, '');
		return here + (here.indexOf('?') === -1 ? '?' : '&') + 'consultation=' + encodeURIComponent(id);
	};

	Consultations.prototype.backLink = function () {
		var a = el('a', 'la-cback', T.cBack);
		a.href = CFG.accountUrl;
		return a;
	};

	/**
	 * The widget opens whatever session is stored, so selecting one here and
	 * going to the chat page lands them on this exact conversation.
	 */
	Consultations.prototype.openChatLink = function (sessionId) {
		var a = el('a', 'la-clink la-clink-chat', T.cOpenChat);
		a.href = CFG.chatUrl || '/';
		a.addEventListener('click', function () {
			write(SESSION_KEY, { id: sessionId });
		});
		return a;
	};

	function fmtDate(iso) {
		if (!iso) return '';
		var d = new Date(iso);
		if (isNaN(d.getTime())) return String(iso);
		try {
			return new Intl.DateTimeFormat('ar', { dateStyle: 'medium', timeStyle: 'short' }).format(d);
		} catch (e) {
			return d.toLocaleString();
		}
	}

	function shortId(id) {
		return String(id || '').slice(-6).toUpperCase();
	}

	function langName(code) {
		if (!code) return null;
		var c = String(code).toLowerCase();
		if (c.indexOf('ar') === 0) return 'العربية';
		if (c.indexOf('en') === 0) return 'English';
		return code;
	}

	function boot() {
		var nodes = document.querySelectorAll('[data-law-agent-chat]');
		for (var i = 0; i < nodes.length; i++) {
			if (!nodes[i].dataset.laMounted) {
				nodes[i].dataset.laMounted = '1';
				new Widget(nodes[i]);
			}
		}
		var pages = document.querySelectorAll('[data-law-agent-consultations]');
		for (var j = 0; j < pages.length; j++) {
			if (!pages[j].dataset.laMounted) {
				pages[j].dataset.laMounted = '1';
				new Consultations(pages[j]);
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
