<?php
/**
 * The Elementor widget itself. Loaded lazily, only once Elementor exists.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class Law_Agent_Elementor_Widget extends \Elementor\Widget_Base {

	public function get_name() {
		return 'law_agent_chat';
	}

	public function get_title() {
		return __( 'Law Agent Chat', 'law-agent-chat' );
	}

	public function get_icon() {
		return 'eicon-comments';
	}

	public function get_categories() {
		return array( 'general' );
	}

	public function get_keywords() {
		return array( 'chat', 'law', 'agent', 'assistant', 'ai', 'مساعد', 'محادثة' );
	}

	/**
	 * Elementor renders a widget after the document head has been sent, so a
	 * stylesheet enqueued during render never reaches the page -- in the
	 * editor the widget came out as a bare textarea and two unlabelled
	 * buttons, which reads as a broken plugin rather than a missing file.
	 *
	 * Declaring the dependency instead lets Elementor enqueue it at the right
	 * moment, in the editor and on the front end alike. The handles are
	 * registered on wp_enqueue_scripts, which runs in both contexts.
	 */
	public function get_style_depends() {
		return array( Law_Agent_Assets::HANDLE );
	}

	public function get_script_depends() {
		return array( Law_Agent_Assets::HANDLE );
	}

	protected function register_controls() {
		$this->start_controls_section(
			'content',
			array( 'label' => __( 'Chat', 'law-agent-chat' ) )
		);

		$this->add_control(
			'height',
			array(
				'label'   => __( 'Height (px)', 'law-agent-chat' ),
				'type'    => \Elementor\Controls_Manager::NUMBER,
				'min'     => 320,
				'max'     => 1400,
				'default' => (int) Law_Agent_Settings::get( 'height' ),
			)
		);

		$this->add_control(
			'fill',
			array(
				'label'        => __( 'Fill container height', 'law-agent-chat' ),
				'description'  => __( 'For popups and any container that sets its own height. Ignores the height above.', 'law-agent-chat' ),
				'type'         => \Elementor\Controls_Manager::SWITCHER,
				'default'      => '',
				'return_value' => 'yes',
			)
		);

		$this->add_control(
			'sidebar',
			array(
				'label'        => __( 'Show conversation list', 'law-agent-chat' ),
				'type'         => \Elementor\Controls_Manager::SWITCHER,
				'default'      => 'yes',
				'return_value' => 'yes',
			)
		);

		$this->add_control(
			'note',
			array(
				'type'            => \Elementor\Controls_Manager::RAW_HTML,
				'raw'             => __( 'Service URLs are set once in Settings → Law Agent Chat.', 'law-agent-chat' ),
				'content_classes' => 'elementor-descriptor',
			)
		);

		$this->end_controls_section();
	}

	protected function render() {
		$s = $this->get_settings_for_display();

		$fill = isset( $s['fill'] ) && 'yes' === $s['fill'];

		echo do_shortcode(
			sprintf(
				'[law_agent_chat height="%s" sidebar="%s"]',
				$fill ? 'fill' : (string) max( 320, absint( isset( $s['height'] ) ? $s['height'] : 620 ) ),
				( isset( $s['sidebar'] ) && 'yes' === $s['sidebar'] ) ? 'yes' : 'no'
			)
		);
	}
}
