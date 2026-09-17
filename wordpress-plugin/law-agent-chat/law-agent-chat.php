<?php
/**
 * Plugin Name:       Law Agent Chat
 * Description:       Embeds the Arabic labour-law assistant. A thin client: the browser talks to the AI service and the product backend directly, because proxying SSE through PHP buffers it.
 * Version:           2.6.2
 * Requires at least: 6.0
 * Requires PHP:      7.4
 * Text Domain:       law-agent-chat
 * Domain Path:       /languages
 *
 * WordPress is the marketing site here, not a tier of the system. This plugin
 * renders a widget, holds two base URLs, and ships the JS client. It never
 * proxies a chat request: generation takes 8-17 seconds of text/event-stream,
 * and PHP would materialise the whole body before forwarding it -- which turns
 * a streamed answer into one lump at the end. Works locally, fails in staging.
 *
 * Identity is never WordPress's. A visitor gets an anonymous token from the
 * product backend on first load; signing up upgrades that same user row in
 * place, so their pre-signup conversations are already theirs.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

define( 'LAW_AGENT_CHAT_VERSION', '2.6.2' );
define( 'LAW_AGENT_CHAT_FILE', __FILE__ );
define( 'LAW_AGENT_CHAT_DIR', plugin_dir_path( __FILE__ ) );
define( 'LAW_AGENT_CHAT_URL', plugin_dir_url( __FILE__ ) );

require_once LAW_AGENT_CHAT_DIR . 'includes/class-law-agent-settings.php';
require_once LAW_AGENT_CHAT_DIR . 'includes/class-law-agent-session.php';
require_once LAW_AGENT_CHAT_DIR . 'includes/class-law-agent-assets.php';
require_once LAW_AGENT_CHAT_DIR . 'includes/class-law-agent-shortcode.php';
require_once LAW_AGENT_CHAT_DIR . 'includes/class-law-agent-elementor.php';
require_once LAW_AGENT_CHAT_DIR . 'includes/class-law-agent-account.php';

add_action(
	'plugins_loaded',
	static function () {
		Law_Agent_Settings::init();
		Law_Agent_Session::init();
		Law_Agent_Assets::init();
		Law_Agent_Shortcode::init();
		Law_Agent_Elementor::init();
		Law_Agent_Account::init();
		load_plugin_textdomain( 'law-agent-chat', false, dirname( plugin_basename( __FILE__ ) ) . '/languages' );
	}
);

/** A settings link on the plugins list, because the widget is useless unconfigured. */
add_filter(
	'plugin_action_links_' . plugin_basename( __FILE__ ),
	static function ( $links ) {
		$url = admin_url( 'options-general.php?page=law-agent-chat' );
		array_unshift( $links, '<a href="' . esc_url( $url ) . '">' . esc_html__( 'Settings', 'law-agent-chat' ) . '</a>' );
		return $links;
	}
);
