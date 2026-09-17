<?php
/**
 * Script and style registration.
 *
 * Registered on wp_enqueue_scripts, enqueued only where the widget actually
 * renders -- a chat client on every page of a marketing site is dead weight.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class Law_Agent_Assets {

	const HANDLE = 'law-agent-chat';

	private static $registered = false;

	public static function init() {
		add_action( 'wp_enqueue_scripts', array( __CLASS__, 'register' ) );

		// Elementor resolves a widget's declared style/script dependencies by
		// handle, so the handle has to exist by then. These fire in the editor
		// preview and on the front end, before widgets render.
		add_action( 'elementor/frontend/after_register_styles', array( __CLASS__, 'register' ) );
		add_action( 'elementor/frontend/after_register_scripts', array( __CLASS__, 'register' ) );
	}

	/**
	 * Idempotent: this is hooked to three actions so the handles exist
	 * whichever path reaches them first. Without the guard, wp_add_inline_script
	 * would append the config object once per call and the page would carry
	 * three copies of it.
	 */
	public static function register() {
		if ( self::$registered ) {
			return;
		}
		self::$registered = true;

		wp_register_style(
			self::HANDLE,
			LAW_AGENT_CHAT_URL . 'assets/law-agent-chat.css',
			array(),
			LAW_AGENT_CHAT_VERSION
		);

		wp_register_script(
			self::HANDLE,
			LAW_AGENT_CHAT_URL . 'assets/law-agent-chat.js',
			array(),
			LAW_AGENT_CHAT_VERSION,
			true
		);

		// Bound at REGISTRATION, not at enqueue.
		//
		// It used to be added inside enqueue(), which the shortcode calls
		// during render — long after wp_enqueue_scripts. In the Elementor
		// editor that inline script never printed, so window.LawAgentChatConfig
		// was undefined and every button rendered with an empty label, because
		// all the visible text comes from i18n. Attaching it to the handle here
		// means it travels with the script whenever and wherever it is
		// enqueued.
		$o = Law_Agent_Settings::get();

		wp_add_inline_script(
			self::HANDLE,
			'window.LawAgentChatConfig = ' . wp_json_encode(
				array(
					'aiUrl'      => $o['ai_url'],
					'backendUrl' => $o['backend_url'],
					// No price here on purpose: the widget asks the backend for
					// it. A price shipped from WordPress could disagree with
					// the one actually charged.
					'consult'    => array(
						'enabled'  => (bool) $o['consult_enabled'],
						'checkout' => $o['paymob_iframe'],
					),
					// Where the widget asks WordPress who is signed in. Same
					// origin, so the auth cookie travels; the shared secret
					// never leaves PHP.
					'sessionEndpoint' => esc_url_raw( rest_url( Law_Agent_Session::NAMESPACE . '/session' ) ),
					// REQUIRED. The REST API does not accept a login cookie on
					// its own: without this nonce, is_user_logged_in() inside a
					// REST request returns false however valid the cookie is,
					// and every signed-in visitor looks anonymous.
					'restNonce'       => wp_create_nonce( 'wp_rest' ),
					'loginUrl'        => $o['login_url'] ? $o['login_url'] : wp_login_url(),
					'registerUrl'     => $o['register_url'],
					// For the consultations page: where to go to book, and where
					// its list lives (empty without WooCommerce).
					'chatUrl'         => $o['chat_url'] ? $o['chat_url'] : home_url( '/' ),
					'accountUrl'      => Law_Agent_Account::list_url(),
					'voiceEnabled'    => (bool) $o['voice_enabled'],
					// Must match VOICE_MAX_SECONDS on the AI service, which
					// refuses longer recordings. The widget stops here so a
					// long question is sent rather than rejected.
					'voiceMaxSeconds' => 60,
					'greeting'   => $o['greeting'],
					'i18n'       => self::strings(),
				)
			) . ';',
			'before'
		);
	}

	/**
	 * Enqueue for this request. Safe to call from a shortcode or an Elementor
	 * widget: WordPress prints late enqueues in the footer, and the config
	 * above is already attached to the handle.
	 */
	public static function enqueue() {
		// Elementor renders widgets in contexts where wp_enqueue_scripts may
		// not have run for this request. Registering twice is harmless;
		// rendering with no stylesheet is not.
		if ( ! wp_script_is( self::HANDLE, 'registered' ) ) {
			self::register();
		}

		wp_enqueue_style( self::HANDLE );
		wp_enqueue_script( self::HANDLE );
	}

	/**
	 * User-facing Arabic copy.
	 *
	 * The API contract is explicit that `message` in an error envelope is for
	 * developers, and that user-facing Arabic belongs in the frontend. This is
	 * that frontend, so every string a visitor can see is here.
	 */
	private static function strings() {
		return array(
			'placeholder'      => __( 'اكتب سؤالك…', 'law-agent-chat' ),
			'send'             => __( 'إرسال', 'law-agent-chat' ),
			'newChat'          => __( 'محادثة جديدة', 'law-agent-chat' ),
			'thinking'         => __( 'جارٍ البحث في المستندات…', 'law-agent-chat' ),
			'sources'          => __( 'المصادر', 'law-agent-chat' ),
			'you'              => __( 'أنت', 'law-agent-chat' ),
			'assistant'        => __( 'المستشار', 'law-agent-chat' ),

			'labelMixed'       => __( 'يتضمن معلومات من خارج المستندات الرسمية', 'law-agent-chat' ),
			'labelModel'       => __( 'لا يستند إلى المستندات الرسمية — قد يكون غير محدَّث', 'law-agent-chat' ),

			'ctaTitle'         => __( 'لم نتمكن من الإجابة على هذا السؤال', 'law-agent-chat' ),
			'ctaBody'          => __( 'يمكنك حجز استشارة مدفوعة مع محامٍ لمتابعة حالتك.', 'law-agent-chat' ),
			/* translators: %s: formatted price, e.g. ٥٠٠٫٠٠ ر.س. */
			'ctaButton'        => __( 'حجز استشارة مدفوعة — %s', 'law-agent-chat' ),
			'ctaButtonPlain'   => __( 'حجز استشارة مدفوعة', 'law-agent-chat' ),

			'signupTitle'      => __( 'أنشئ حسابًا لإتمام الحجز', 'law-agent-chat' ),
			'signupBody'       => __( 'محادثتك الحالية ستبقى محفوظة في حسابك.', 'law-agent-chat' ),
			'signupName'       => __( 'الاسم', 'law-agent-chat' ),
			'signupEmail'      => __( 'البريد الإلكتروني', 'law-agent-chat' ),
			'signupPassword'   => __( 'كلمة المرور (١٠ أحرف على الأقل)', 'law-agent-chat' ),
			'signupSubmit'     => __( 'متابعة إلى الدفع', 'law-agent-chat' ),
			'signupCancel'     => __( 'إلغاء', 'law-agent-chat' ),
			'signupWorking'    => __( 'جارٍ التحويل إلى صفحة الدفع…', 'law-agent-chat' ),

			'loginTitle'       => __( 'سجّل الدخول لإتمام الحجز', 'law-agent-chat' ),
			'loginBody'        => __( 'محادثتك الحالية ستبقى محفوظة وتُربط بحسابك بعد تسجيل الدخول.', 'law-agent-chat' ),
			'loginButton'      => __( 'تسجيل الدخول', 'law-agent-chat' ),
			'registerButton'   => __( 'إنشاء حساب جديد', 'law-agent-chat' ),

			'escalated'        => __( 'تم تحويل هذه المحادثة إلى محامٍ. لن يجيب المساعد الآلي عليها بعد الآن.', 'law-agent-chat' ),
			'errEmailTaken'    => __( 'هذا البريد الإلكتروني مستخدم بالفعل. سجّل الدخول لإتمام الحجز.', 'law-agent-chat' ),
			'errOpenConsult'   => __( 'لديك استشارة قيد التنفيذ بالفعل.', 'law-agent-chat' ),
			'errNotReady'      => __( 'الخدمة غير متاحة حاليًا. حاول بعد قليل.', 'law-agent-chat' ),
			'errAuth'          => __( 'انتهت الجلسة. أعد تحميل الصفحة.', 'law-agent-chat' ),
			'errNetwork'       => __( 'تعذّر الاتصال بالخدمة. تحقّق من اتصالك وحاول مجددًا.', 'law-agent-chat' ),
			'errGeneric'       => __( 'حدث خطأ غير متوقع. حاول مجددًا.', 'law-agent-chat' ),
			'errUnconfigured'  => __( 'لم يتم إعداد المساعد بعد.', 'law-agent-chat' ),
			'retry'            => __( 'إعادة المحاولة', 'law-agent-chat' ),

			/* translators: %s: number of messages left */
			'remaining'        => __( 'الرسائل المتبقية: %s', 'law-agent-chat' ),
			'quotaAnonTitle'   => __( 'انتهت رسائلك المجانية', 'law-agent-chat' ),
			/* translators: %s: number of extra messages an account gives */
			'quotaAnonBody'    => __( 'سجّل دخولك لتحصل على %s رسالة إضافية.', 'law-agent-chat' ),
			'quotaUserTitle'   => __( 'وصلت إلى الحد الأقصى من الرسائل', 'law-agent-chat' ),
			'quotaUserBody'    => __( 'لمتابعة حالتك، يمكنك حجز استشارة مع محامٍ.', 'law-agent-chat' ),

			'micStart'         => __( 'تسجيل صوتي', 'law-agent-chat' ),
			'micStop'          => __( 'إيقاف التسجيل', 'law-agent-chat' ),
			'transcribing'     => __( 'جارٍ تحويل الصوت إلى نص…', 'law-agent-chat' ),
			'micDenied'        => __( 'لم نتمكن من الوصول إلى الميكروفون. اسمح بالوصول من إعدادات المتصفح.', 'law-agent-chat' ),
			'noSpeech'         => __( 'لم نسمع كلامًا واضحًا. حاول مرة أخرى.', 'law-agent-chat' ),
			'speechDown'       => __( 'خدمة الصوت غير متاحة حاليًا. اكتب سؤالك بدلًا من ذلك.', 'law-agent-chat' ),
			'listen'           => __( 'استمع', 'law-agent-chat' ),

			/* the consultations page */
			'cTitle'           => __( 'استشاراتي', 'law-agent-chat' ),
			'cEmpty'           => __( 'لم تحجز أي استشارة بعد.', 'law-agent-chat' ),
			'cLoadError'       => __( 'تعذّر تحميل الاستشارات. حاول لاحقًا.', 'law-agent-chat' ),
			'cNotFound'        => __( 'لم نعثر على هذه الاستشارة.', 'law-agent-chat' ),
			'cBack'            => __( 'العودة إلى استشاراتي', 'law-agent-chat' ),
			'cNumber'          => __( 'رقم الاستشارة', 'law-agent-chat' ),
			'cDate'            => __( 'التاريخ', 'law-agent-chat' ),
			'cStatus'          => __( 'الحالة', 'law-agent-chat' ),
			'cAmount'          => __( 'المبلغ', 'law-agent-chat' ),
			'cPaidAt'          => __( 'تاريخ الدفع', 'law-agent-chat' ),
			'cLawyer'          => __( 'المحامي', 'law-agent-chat' ),
			'cLanguage'        => __( 'لغة المحادثة', 'law-agent-chat' ),
			'cSummary'         => __( 'ملخص الحالة المرسل للمحامي', 'law-agent-chat' ),
			'cTranscript'      => __( 'المحادثة', 'law-agent-chat' ),
			'cDetails'         => __( 'التفاصيل', 'law-agent-chat' ),
			'cOpenChat'        => __( 'افتح المحادثة', 'law-agent-chat' ),
			'cNoSession'       => __( 'لا توجد محادثة مرتبطة بهذه الاستشارة.', 'law-agent-chat' ),
			'cTranscriptError' => __( 'تعذّر تحميل المحادثة.', 'law-agent-chat' ),
			'cStatusPending'   => __( 'بانتظار الدفع', 'law-agent-chat' ),
			'cStatusPaid'      => __( 'مدفوعة', 'law-agent-chat' ),
			'cStatusCancelled' => __( 'ملغاة', 'law-agent-chat' ),
			'cStatusRefunded'  => __( 'مستردة', 'law-agent-chat' ),
			'cEscalated'       => __( 'تم إرسالها للمحامي', 'law-agent-chat' ),
			'cEscalating'      => __( 'قيد الإرسال للمحامي', 'law-agent-chat' ),
			'cNotEscalated'    => __( 'لم تُرسل بعد', 'law-agent-chat' ),
			'cBook'            => __( 'حجز استشارة', 'law-agent-chat' ),
			'stopListening'    => __( 'إيقاف', 'law-agent-chat' ),
			'loadingAudio'     => __( 'جارٍ التحميل…', 'law-agent-chat' ),
		);
	}
}
