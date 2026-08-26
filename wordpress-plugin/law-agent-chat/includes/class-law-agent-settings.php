<?php
/**
 * Settings: the two base URLs and the consultation offer.
 *
 * Nothing secret lives here. The admin API key is deliberately absent -- the
 * service-to-service endpoints (erasure, admin escalate, admin billing) must
 * never be reachable from a browser, so this plugin has no way to call them.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class Law_Agent_Settings {

	const OPTION = 'law_agent_chat_options';

	public static function init() {
		add_action( 'admin_menu', array( __CLASS__, 'menu' ) );
		add_action( 'admin_init', array( __CLASS__, 'register' ) );
	}

	public static function defaults() {
		return array(
			'ai_url'           => 'http://127.0.0.1:8000',
			'backend_url'      => 'http://127.0.0.1:8001',
			'consult_enabled'  => 1,
			'paymob_iframe'    => 'https://ksa.paymob.com/api/acceptance/iframes/13464',
			'shared_secret'    => '',
			'login_url'        => '',
			'register_url'     => '',
			'greeting'         => '',
			'height'           => 620,
		);
	}

	public static function get( $key = null ) {
		$opts = wp_parse_args( get_option( self::OPTION, array() ), self::defaults() );
		return null === $key ? $opts : ( isset( $opts[ $key ] ) ? $opts[ $key ] : null );
	}

	public static function menu() {
		add_options_page(
			__( 'Law Agent Chat', 'law-agent-chat' ),
			__( 'Law Agent Chat', 'law-agent-chat' ),
			'manage_options',
			'law-agent-chat',
			array( __CLASS__, 'page' )
		);
	}

	public static function register() {
		register_setting(
			'law_agent_chat',
			self::OPTION,
			array( 'sanitize_callback' => array( __CLASS__, 'sanitize' ) )
		);
	}

	public static function sanitize( $input ) {
		$out = self::defaults();
		$in  = is_array( $input ) ? $input : array();

		// Both must be absolute origins the *browser* can reach. 127.0.0.1 is
		// the server's loopback, not the visitor's -- it only works while you
		// are testing on the machine running the services.
		$out['ai_url']      = untrailingslashit( esc_url_raw( trim( (string) ( isset( $in['ai_url'] ) ? $in['ai_url'] : '' ) ) ) );
		$out['backend_url'] = untrailingslashit( esc_url_raw( trim( (string) ( isset( $in['backend_url'] ) ? $in['backend_url'] : '' ) ) ) );

		// No price field. The backend quotes it (GET /consultations/price) and
		// charges from its own configuration, so a number stored here could
		// only ever disagree with the invoice.
		$out['consult_enabled'] = empty( $in['consult_enabled'] ) ? 0 : 1;
		$out['paymob_iframe']   = untrailingslashit( esc_url_raw( trim( (string) ( isset( $in['paymob_iframe'] ) ? $in['paymob_iframe'] : '' ) ) ) );
		// Trimmed only. It is a shared secret, not a display string: sanitising
		// it would silently change the bytes being signed and every assertion
		// would fail verification for no visible reason.
		// Empty means wp-login.php. A site with a branded Arabic login page
		// should point here instead: sending someone from an Arabic legal
		// service to the default WordPress form is a visible seam.
		$out['login_url']       = esc_url_raw( trim( (string) ( isset( $in['login_url'] ) ? $in['login_url'] : '' ) ) );
		// Optional second page. Sites that split sign-in from sign-up want the
		// widget to offer both, because a first-time visitor sent to a login
		// form has to find the register link themselves.
		$out['register_url']    = esc_url_raw( trim( (string) ( isset( $in['register_url'] ) ? $in['register_url'] : '' ) ) );
		$out['shared_secret']   = trim( (string) ( isset( $in['shared_secret'] ) ? $in['shared_secret'] : '' ) );
		$out['greeting']        = sanitize_textarea_field( (string) ( isset( $in['greeting'] ) ? $in['greeting'] : '' ) );
		$out['height']          = max( 320, absint( isset( $in['height'] ) ? $in['height'] : 620 ) );

		return $out;
	}

	/** This site's scheme://host[:port] -- what the services must allow in CORS. */
	public static function site_origin() {
		$parts  = wp_parse_url( home_url() );
		$scheme = isset( $parts['scheme'] ) ? $parts['scheme'] : 'https';
		$host   = isset( $parts['host'] ) ? $parts['host'] : '';
		$port   = isset( $parts['port'] ) ? ':' . $parts['port'] : '';
		return $scheme . '://' . $host . $port;
	}

	public static function page() {
		if ( ! current_user_can( 'manage_options' ) ) {
			return;
		}
		$o      = self::get();
		$option = self::OPTION;
		$origin = self::site_origin();
		?>
		<div class="wrap">
			<h1><?php esc_html_e( 'Law Agent Chat', 'law-agent-chat' ); ?></h1>

			<p>
				<?php esc_html_e( 'Embed the assistant with the shortcode', 'law-agent-chat' ); ?>
				<code>[law_agent_chat]</code>
				<?php esc_html_e( 'or the "Law Agent Chat" Elementor widget.', 'law-agent-chat' ); ?>
			</p>

			<form method="post" action="options.php">
				<?php settings_fields( 'law_agent_chat' ); ?>
				<table class="form-table" role="presentation">
					<tr>
						<th scope="row"><label for="la-ai"><?php esc_html_e( 'AI service URL', 'law-agent-chat' ); ?></label></th>
						<td>
							<input id="la-ai" class="regular-text code" type="url" name="<?php echo esc_attr( $option ); ?>[ai_url]" value="<?php echo esc_attr( $o['ai_url'] ); ?>">
							<p class="description"><?php esc_html_e( 'The Python service: chat and streaming. The visitor\'s browser connects to it directly, so it must be publicly reachable over HTTPS.', 'law-agent-chat' ); ?></p>
						</td>
					</tr>
					<tr>
						<th scope="row"><label for="la-be"><?php esc_html_e( 'Product backend URL', 'law-agent-chat' ); ?></label></th>
						<td>
							<input id="la-be" class="regular-text code" type="url" name="<?php echo esc_attr( $option ); ?>[backend_url]" value="<?php echo esc_attr( $o['backend_url'] ); ?>">
							<p class="description"><?php esc_html_e( 'The Node service: identity and consultations.', 'law-agent-chat' ); ?></p>
						</td>
					</tr>
					<tr>
						<th scope="row"><?php esc_html_e( 'Paid consultation', 'law-agent-chat' ); ?></th>
						<td>
							<label>
								<input type="checkbox" name="<?php echo esc_attr( $option ); ?>[consult_enabled]" value="1" <?php checked( $o['consult_enabled'], 1 ); ?>>
								<?php esc_html_e( 'Offer a paid consultation when the assistant declines to answer', 'law-agent-chat' ); ?>
							</label>
							<p class="description"><?php esc_html_e( 'Shown on answers labelled "refused". Buying requires an account, so anonymous visitors are asked to sign up first -- that signup upgrades their existing user row, so the conversation they already had stays theirs.', 'law-agent-chat' ); ?></p>
						</td>
					</tr>
					<tr>
						<th scope="row"><?php esc_html_e( 'Price', 'law-agent-chat' ); ?></th>
						<td>
							<p class="description">
								<?php esc_html_e( 'Set on the backend, not here: CONSULTATION_PRICE_CENTS and CONSULTATION_CURRENCY. The widget asks it for the price and displays whatever it is told, so the figure on the button is always the figure charged.', 'law-agent-chat' ); ?>
							</p>
						</td>
					</tr>
					<tr>
						<th scope="row"><label for="la-iframe"><?php esc_html_e( 'Paymob checkout URL', 'law-agent-chat' ); ?></label></th>
						<td>
							<input id="la-iframe" class="regular-text code" type="url" name="<?php echo esc_attr( $option ); ?>[paymob_iframe]" value="<?php echo esc_attr( $o['paymob_iframe'] ); ?>">
							<p class="description"><?php esc_html_e( 'The visitor is sent here with ?payment_token= appended. Must match the Paymob region the backend is configured for.', 'law-agent-chat' ); ?></p>
						</td>
					</tr>
					<tr>
						<th scope="row"><label for="la-login"><?php esc_html_e( 'Login page', 'law-agent-chat' ); ?></label></th>
						<td>
							<input id="la-login" class="regular-text code" type="url" name="<?php echo esc_attr( $option ); ?>[login_url]" value="<?php echo esc_attr( $o['login_url'] ); ?>" placeholder="<?php echo esc_attr( wp_login_url() ); ?>">
							<p class="description">
								<?php esc_html_e( 'Where a logged-out visitor is sent when they try to book. Leave empty for the default WordPress login form.', 'law-agent-chat' ); ?>
								<?php esc_html_e( 'The page they were on is appended as redirect_to, so they come back to their conversation.', 'law-agent-chat' ); ?>
							</p>
						</td>
					</tr>
					<tr>
						<th scope="row"><label for="la-register"><?php esc_html_e( 'Register page', 'law-agent-chat' ); ?></label></th>
						<td>
							<input id="la-register" class="regular-text code" type="url" name="<?php echo esc_attr( $option ); ?>[register_url]" value="<?php echo esc_attr( $o['register_url'] ); ?>">
							<p class="description"><?php esc_html_e( 'Optional. If your sign-up lives on its own page, the widget offers it alongside sign-in. Leave empty to show only the login link.', 'law-agent-chat' ); ?></p>
						</td>
					</tr>
					<tr>
						<th scope="row"><label for="la-secret"><?php esc_html_e( 'Shared secret', 'law-agent-chat' ); ?></label></th>
						<td>
							<?php if ( defined( 'LAW_AGENT_SHARED_SECRET' ) && LAW_AGENT_SHARED_SECRET ) : ?>
								<p class="description">
									<strong><?php esc_html_e( 'Set in wp-config.php.', 'law-agent-chat' ); ?></strong>
									<?php esc_html_e( 'That constant wins over this field, which is the safer place for it.', 'law-agent-chat' ); ?>
								</p>
							<?php else : ?>
								<input id="la-secret" class="regular-text code" type="password" autocomplete="off"
									name="<?php echo esc_attr( $option ); ?>[shared_secret]"
									value="<?php echo esc_attr( $o['shared_secret'] ); ?>">
								<p class="description">
									<?php esc_html_e( 'Must match WORDPRESS_SHARED_SECRET on the product backend. This is how WordPress proves a visitor is signed in.', 'law-agent-chat' ); ?>
									<br>
									<strong><?php esc_html_e( 'Better:', 'law-agent-chat' ); ?></strong>
									<?php esc_html_e( 'put it in wp-config.php instead — a database dump is a far more common thing to hand around than a wp-config.', 'law-agent-chat' ); ?>
									<code>define( 'LAW_AGENT_SHARED_SECRET', '…' );</code>
								</p>
							<?php endif; ?>
						</td>
					</tr>
					<tr>
						<th scope="row"><?php esc_html_e( 'Secret fingerprint', 'law-agent-chat' ); ?></th>
						<td>
							<?php $fp = Law_Agent_Session::secret_fingerprint(); ?>
							<?php if ( $fp ) : ?>
								<code><?php echo esc_html( $fp ); ?></code>
								<p class="description"><?php esc_html_e( 'Compare with `npm run secret:fingerprint` on the backend. If these differ, the assertion is rejected as "bad signature" — which looks identical to being logged out.', 'law-agent-chat' ); ?></p>
							<?php else : ?>
								<p class="description"><strong><?php esc_html_e( 'No secret set.', 'law-agent-chat' ); ?></strong> <?php esc_html_e( 'WordPress logins cannot be verified until one is.', 'law-agent-chat' ); ?></p>
							<?php endif; ?>
						</td>
					</tr>
					<tr>
						<th scope="row"><label for="la-greet"><?php esc_html_e( 'Opening message', 'law-agent-chat' ); ?></label></th>
						<td>
							<textarea id="la-greet" class="large-text" rows="2" name="<?php echo esc_attr( $option ); ?>[greeting]"><?php echo esc_textarea( $o['greeting'] ); ?></textarea>
							<p class="description"><?php esc_html_e( 'Shown before the first question. Never sent to the model. Leave empty for none.', 'law-agent-chat' ); ?></p>
						</td>
					</tr>
					<tr>
						<th scope="row"><label for="la-h"><?php esc_html_e( 'Default height (px)', 'law-agent-chat' ); ?></label></th>
						<td><input id="la-h" type="number" min="320" name="<?php echo esc_attr( $option ); ?>[height]" value="<?php echo esc_attr( $o['height'] ); ?>"></td>
					</tr>
				</table>
				<?php submit_button(); ?>
			</form>

			<hr>

			<h2><?php esc_html_e( 'What the services need from you', 'law-agent-chat' ); ?></h2>
			<p><?php esc_html_e( 'Both services allow localhost only until this site\'s origin is set as CORS_ORIGIN_REGEX on each of them:', 'law-agent-chat' ); ?></p>
			<p><code>CORS_ORIGIN_REGEX=<?php echo esc_html( str_replace( array( '.', ':', '/' ), array( '\\.', '\\:', '\\/' ), $origin ) ); ?></code></p>
			<p><?php esc_html_e( 'Whatever sits in front of the AI service also needs proxy_buffering off, or the answer arrives as one lump after 17 seconds instead of streaming.', 'law-agent-chat' ); ?></p>
		</div>
		<?php
	}
}
