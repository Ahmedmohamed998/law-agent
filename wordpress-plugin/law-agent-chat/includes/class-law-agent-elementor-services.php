<?php
/**
 * The services cards as an Elementor widget. Renders the shortcode.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class Law_Agent_Elementor_Services extends \Elementor\Widget_Base {

	public function get_name() {
		return 'law_agent_services';
	}

	public function get_title() {
		return __( 'Law Agent Services', 'law-agent-chat' );
	}

	public function get_icon() {
		return 'eicon-price-table';
	}

	public function get_categories() {
		return array( 'general' );
	}

	public function get_keywords() {
		return array( 'services', 'law', 'agent', 'price', 'خدمات', 'استشارة' );
	}

	public function get_style_depends() {
		return array( Law_Agent_Assets::HANDLE );
	}

	public function get_script_depends() {
		return array( Law_Agent_Assets::HANDLE );
	}

	protected function register_controls() {
		$this->start_controls_section(
			'content',
			array( 'label' => __( 'Services', 'law-agent-chat' ) )
		);

		$this->add_control(
			'columns',
			array(
				'label'   => __( 'Columns', 'law-agent-chat' ),
				'type'    => \Elementor\Controls_Manager::NUMBER,
				'min'     => 1,
				'max'     => 4,
				'default' => 3,
			)
		);

		$this->add_control(
			'only',
			array(
				'label'       => __( 'Only these services', 'law-agent-chat' ),
				'description' => __( 'Comma-separated slugs from the dashboard, e.g. contract-review,dispute. Empty shows every active service.', 'law-agent-chat' ),
				'type'        => \Elementor\Controls_Manager::TEXT,
				'default'     => '',
			)
		);

		$this->add_control(
			'note',
			array(
				'type'            => \Elementor\Controls_Manager::RAW_HTML,
				'raw'             => __( 'Names, descriptions and prices come from the admin dashboard. Nothing here is edited in WordPress.', 'law-agent-chat' ),
				'content_classes' => 'elementor-descriptor',
			)
		);

		$this->end_controls_section();
	}

	protected function render() {
		$s = $this->get_settings_for_display();
		echo do_shortcode(
			sprintf(
				'[law_agent_services columns="%d" only="%s"]',
				isset( $s['columns'] ) ? absint( $s['columns'] ) : 3,
				esc_attr( isset( $s['only'] ) ? (string) $s['only'] : '' )
			)
		);
	}
}
