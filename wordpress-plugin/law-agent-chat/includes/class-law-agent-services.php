<?php
/**
 * [law_agent_services] -- the firm's services as cards a client can order.
 *
 * The cards are rendered in the browser from the backend's GET /services,
 * so the figure on a card is the figure charged; nothing about a service is
 * stored in WordPress. Ordering runs through the same flow the chat uses:
 * sign in first, describe the case if the service asks for it, then Paymob.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class Law_Agent_Services {

	public static function init() {
		add_shortcode( 'law_agent_services', array( __CLASS__, 'render' ) );
	}

	/**
	 * Attributes:
	 *   columns  1-4, default 3
	 *   only     comma-separated slugs to show, default all active
	 */
	public static function render( $atts = array() ) {
		$o = Law_Agent_Settings::get();
		if ( '' === $o['ai_url'] || '' === $o['backend_url'] ) {
			return '';
		}
		if ( ! $o['consult_enabled'] ) {
			return '';
		}

		$atts = shortcode_atts(
			array(
				'columns' => 3,
				'only'    => '',
			),
			$atts,
			'law_agent_services'
		);

		Law_Agent_Assets::enqueue();

		$columns = min( 4, max( 1, absint( $atts['columns'] ) ) );
		$only    = preg_replace( '/[^a-z0-9,-]/', '', strtolower( (string) $atts['only'] ) );

		return '<div class="law-agent-services" dir="rtl" lang="ar" data-law-agent-services'
			. ' data-columns="' . esc_attr( $columns ) . '"'
			. ( '' !== $only ? ' data-only="' . esc_attr( $only ) . '"' : '' )
			. '><div class="la-account-loading">' . esc_html__( 'جارٍ التحميل…', 'law-agent-chat' ) . '</div></div>';
	}
}
