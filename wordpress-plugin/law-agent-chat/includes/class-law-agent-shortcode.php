<?php
/**
 * [law_agent_chat] -- the markup the JS client mounts into.
 *
 * The container carries dir="rtl" and lang="ar" itself rather than inheriting
 * them, because the widget is very often dropped onto an LTR marketing page.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class Law_Agent_Shortcode {

	public static function init() {
		add_shortcode( 'law_agent_chat', array( __CLASS__, 'render' ) );
	}

	public static function render( $atts = array() ) {
		$o = Law_Agent_Settings::get();

		$atts = shortcode_atts(
			array(
				'height'  => $o['height'],
				'sidebar' => 'yes',
				'class'   => '',
			),
			$atts,
			'law_agent_chat'
		);

		if ( '' === $o['ai_url'] || '' === $o['backend_url'] ) {
			if ( current_user_can( 'manage_options' ) ) {
				return '<p class="law-agent-notice">' . esc_html__( 'Law Agent Chat: set the AI service and product backend URLs in Settings → Law Agent Chat.', 'law-agent-chat' ) . '</p>';
			}
			return '';
		}

		Law_Agent_Assets::enqueue();

		// height="fill" makes the widget take its container's height instead
		// of a fixed one -- what an Elementor popup or a flex cell wants,
		// where a pixel height just fights the box it was put in.
		$fill    = 'fill' === strtolower( trim( (string) $atts['height'] ) );
		$height  = $fill ? 0 : max( 320, absint( $atts['height'] ) );
		$sidebar = in_array( strtolower( $atts['sidebar'] ), array( 'yes', 'true', '1' ), true );

		$classes = 'law-agent-chat';
		if ( $fill ) {
			$classes .= ' is-fill';
		}
		if ( $sidebar ) {
			$classes .= ' has-sidebar';
		}
		if ( $atts['class'] ) {
			$classes .= ' ' . sanitize_html_class( $atts['class'] );
		}

		ob_start();
		?>
		<div class="<?php echo esc_attr( $classes ); ?>"
			dir="rtl"
			lang="ar"
			<?php if ( ! $fill ) : ?>style="--law-agent-height: <?php echo esc_attr( $height ); ?>px"<?php endif; ?>
			data-law-agent-chat>

			<?php if ( $sidebar ) : ?>
				<aside class="la-sidebar">
					<button type="button" class="la-new" data-la-new></button>
					<div class="la-sessions" data-la-sessions></div>
				</aside>
			<?php endif; ?>

			<div class="la-main">
				<div class="la-log" data-la-log>
					<div class="la-thread" data-la-thread></div>
				</div>

				<!--
					Always-available booking entry point. The inline CTA only
					appears on a `refused` answer, but people ask to book
					outright -- and the model itself replies "press the button
					below", which has to be true whichever answer it came from.
					Hidden until the JS confirms consultations are enabled.
				-->
				<div class="la-bookbar" data-la-bookbar hidden>
					<button type="button" class="la-book" data-la-book></button>
				</div>

				<!--
					onsubmit="return false" is a floor, not the mechanism: the
					JS binds submit and calls preventDefault. But if it has not
					mounted yet -- a popup that injected this markup before the
					script ran, a JS error, an optimiser that mangled the file
					-- a native submit reloads the page and loses the
					conversation. A widget that fails should do nothing, not
					destroy what the visitor was doing.
				-->
				<form class="la-composer" data-la-composer onsubmit="return false">
					<textarea class="la-input" data-la-input rows="2"></textarea>
					<button type="submit" class="la-send" data-la-send></button>
				</form>
			</div>
		</div>
		<?php
		return ob_get_clean();
	}
}
