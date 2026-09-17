<?php
/**
 * "استشاراتي" -- the signed-in client's consultations, in WooCommerce's
 * My Account and as a shortcode.
 *
 * Rendered in the browser from GET /consultations on the product backend,
 * with the visitor's own token -- the same token the chat widget holds. PHP
 * fetches nothing: it would need a token of its own for every page view,
 * and the backend already scopes that route to the caller, so the page can
 * only ever show the visitor their own rows.
 *
 * Not WooCommerce orders. A consultation is priced and settled by the
 * backend; mirroring it into wc_orders would make two records of one
 * payment that can disagree.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class Law_Agent_Account {

	/** The My Account endpoint: /my-account/consultations/[id]/ */
	const ENDPOINT = 'consultations';

	/**
	 * Bumped whenever the endpoint changes. Rewrite rules are cached, and
	 * "Replace current with uploaded" does not run the activation hook, so
	 * the flush is keyed to this instead of to activation.
	 */
	const REWRITE_VERSION = '1';

	public static function init() {
		add_action( 'init', array( __CLASS__, 'endpoint' ) );
		add_shortcode( 'law_agent_consultations', array( __CLASS__, 'shortcode' ) );

		// WooCommerce may not be active; every hook below is a no-op then
		// and the shortcode still works on any page.
		add_filter( 'woocommerce_account_menu_items', array( __CLASS__, 'menu_item' ) );
		add_action( 'woocommerce_account_' . self::ENDPOINT . '_endpoint', array( __CLASS__, 'account_tab' ) );
		add_filter( 'woocommerce_endpoint_' . self::ENDPOINT . '_title', array( __CLASS__, 'title' ) );
	}

	public static function endpoint() {
		add_rewrite_endpoint( self::ENDPOINT, EP_ROOT | EP_PAGES );

		if ( get_option( 'law_agent_chat_rewrite' ) !== self::REWRITE_VERSION ) {
			flush_rewrite_rules( false );
			update_option( 'law_agent_chat_rewrite', self::REWRITE_VERSION );
		}
	}

	/** After "Orders", where a client looks for things they paid for. */
	public static function menu_item( $items ) {
		$out = array();
		foreach ( $items as $key => $label ) {
			$out[ $key ] = $label;
			if ( 'orders' === $key ) {
				$out[ self::ENDPOINT ] = self::title();
			}
		}
		if ( ! isset( $out[ self::ENDPOINT ] ) ) {
			$out[ self::ENDPOINT ] = self::title();
		}
		return $out;
	}

	public static function title() {
		return __( 'استشاراتي', 'law-agent-chat' );
	}

	/** The endpoint's value is a consultation id, or empty for the list. */
	public static function account_tab() {
		$value = get_query_var( self::ENDPOINT );
		echo self::render( is_string( $value ) ? $value : '' ); // phpcs:ignore WordPress.Security.EscapeOutput -- built with esc_* below
	}

	public static function shortcode( $atts = array() ) {
		$atts = shortcode_atts( array( 'id' => '' ), $atts, 'law_agent_consultations' );
		$id   = $atts['id'];
		if ( '' === $id && isset( $_GET['consultation'] ) ) { // phpcs:ignore WordPress.Security.NonceVerification -- read-only selector
			$id = sanitize_text_field( wp_unslash( $_GET['consultation'] ) );
		}
		return self::render( $id );
	}

	/**
	 * The container the JS mounts into. Signed-out visitors get the login
	 * link rather than an empty list: the backend would only answer with
	 * an anonymous user's consultations, of which there are never any.
	 */
	public static function render( $consultation_id = '' ) {
		$o = Law_Agent_Settings::get();
		if ( '' === $o['ai_url'] || '' === $o['backend_url'] ) {
			return '';
		}

		if ( ! is_user_logged_in() ) {
			$login = $o['login_url'] ? $o['login_url'] : wp_login_url();
			$login = add_query_arg( 'redirect_to', rawurlencode( self::current_url() ), $login );
			return '<div class="law-agent-account" dir="rtl" lang="ar"><p class="la-account-empty">'
				. esc_html__( 'سجّل دخولك لعرض استشاراتك.', 'law-agent-chat' )
				. ' <a class="la-cta-button" href="' . esc_url( $login ) . '">' . esc_html__( 'تسجيل الدخول', 'law-agent-chat' ) . '</a></p></div>';
		}

		Law_Agent_Assets::enqueue();

		$id = preg_replace( '/[^A-Za-z0-9_-]/', '', (string) $consultation_id );

		return '<div class="law-agent-account" dir="rtl" lang="ar" data-law-agent-consultations'
			. ( '' !== $id ? ' data-consultation="' . esc_attr( $id ) . '"' : '' )
			. '><div class="la-account-loading">' . esc_html__( 'جارٍ التحميل…', 'law-agent-chat' ) . '</div></div>';
	}

	/** Base URL for the list, so a detail view can link back to it. */
	public static function list_url() {
		if ( function_exists( 'wc_get_account_endpoint_url' ) && function_exists( 'wc_get_page_id' ) && wc_get_page_id( 'myaccount' ) > 0 ) {
			return wc_get_account_endpoint_url( self::ENDPOINT );
		}
		return '';
	}

	private static function current_url() {
		$path = isset( $_SERVER['REQUEST_URI'] ) ? wp_unslash( $_SERVER['REQUEST_URI'] ) : '/'; // phpcs:ignore WordPress.Security.ValidatedSanitizedInput
		return home_url( $path );
	}
}
