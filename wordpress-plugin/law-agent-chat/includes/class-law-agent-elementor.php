<?php
/**
 * Elementor widget, registered only when Elementor is active.
 *
 * It renders the shortcode rather than duplicating its markup, so there is one
 * template to keep correct.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class Law_Agent_Elementor {

	public static function init() {
		add_action( 'elementor/widgets/register', array( __CLASS__, 'register' ) );
	}

	public static function register( $widgets_manager ) {
		if ( ! did_action( 'elementor/loaded' ) || ! class_exists( '\Elementor\Widget_Base' ) ) {
			return;
		}
		require_once LAW_AGENT_CHAT_DIR . 'includes/class-law-agent-elementor-widget.php';
		require_once LAW_AGENT_CHAT_DIR . 'includes/class-law-agent-elementor-services.php';
		$widgets_manager->register( new Law_Agent_Elementor_Widget() );
		$widgets_manager->register( new Law_Agent_Elementor_Services() );
	}
}
