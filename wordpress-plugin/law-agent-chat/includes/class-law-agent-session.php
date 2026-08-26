<?php
/**
 * The bridge: proving to the backend that this visitor is logged into
 * WordPress.
 *
 * WordPress owns the password, the reset flow and email verification. It does
 * NOT own tokens -- it has no signing key and giving it one would mean a
 * second runtime able to sign for the identity domain. Instead it mints a
 * short-lived assertion over a shared secret, and the backend exchanges that
 * for its own RS256 token.
 *
 * The assertion is built HERE, in PHP, and never in JavaScript. The shared
 * secret is the whole trust of this bridge: anything that can read it can mint
 * an assertion for any user id, including an administrator's.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class Law_Agent_Session {

	const NAMESPACE = 'law-agent/v1';

	/** Sixty seconds: long enough for a page load, short enough to be dull if leaked. */
	const TTL = 60;

	public static function init() {
		add_action( 'rest_api_init', array( __CLASS__, 'routes' ) );
	}

	public static function routes() {
		register_rest_route(
			self::NAMESPACE,
			'/session',
			array(
				'methods'             => 'GET',
				'callback'            => array( __CLASS__, 'session' ),
				// Deliberately open: the callback distinguishes logged-in from
				// logged-out itself and returns an assertion only for the
				// former. A permission_callback that rejected anonymous
				// visitors would make the widget unable to ask.
				'permission_callback' => '__return_true',
			)
		);
	}

	/**
	 * The signing secret.
	 *
	 * Prefers a wp-config.php constant over the options table: a database dump
	 * is a far more common thing to hand around than a wp-config, and a secret
	 * in an option is a secret in every backup and every migration export.
	 */
	public static function secret() {
		// Trimmed on both paths. A secret pasted into wp-config.php or a
		// settings field arrives with a trailing space or newline more often
		// than anyone expects, and the only symptom is "bad signature" on the
		// other side -- indistinguishable from a genuinely wrong value.
		if ( defined( 'LAW_AGENT_SHARED_SECRET' ) && trim( (string) LAW_AGENT_SHARED_SECRET ) !== '' ) {
			return trim( (string) LAW_AGENT_SHARED_SECRET );
		}
		return trim( (string) Law_Agent_Settings::get( 'shared_secret' ) );
	}

	/**
	 * A fingerprint of the secret in use, for the settings screen.
	 *
	 * Not the secret itself: enough to compare against the other side without
	 * putting the value on a page that a shoulder or a screenshot can read.
	 */
	public static function secret_fingerprint() {
		$secret = self::secret();
		if ( '' === $secret ) {
			return '';
		}
		return strtoupper( substr( hash( 'sha256', $secret ), 0, 12 ) ) . ' (' . strlen( $secret ) . ' chars)';
	}

	private static function b64url( $bytes ) {
		return rtrim( strtr( base64_encode( $bytes ), '+/', '-_' ), '=' );
	}

	/**
	 * GET /wp-json/law-agent/v1/session
	 *
	 * { logged_in: false }                      for a visitor
	 * { logged_in: true, assertion, expires_in } for a member
	 */
	public static function session( WP_REST_Request $request ) {
		if ( ! is_user_logged_in() ) {
			return new WP_REST_Response( array( 'logged_in' => false ), 200 );
		}

		$secret = self::secret();
		if ( '' === $secret ) {
			// A misconfiguration, not a rejection. Say so plainly rather than
			// letting the widget conclude the user is logged out.
			return new WP_REST_Response(
				array(
					'logged_in' => true,
					'error'     => 'law_agent_unconfigured',
					'message'   => 'The Law Agent shared secret is not set.',
				),
				503
			);
		}

		$user = wp_get_current_user();
		$now  = time();

		$payload = array(
			// The identity is the WordPress user id, never the email: an
			// email is something a person changes, and the link must survive
			// that.
			'wp_user_id'   => (int) $user->ID,
			'email'        => $user->user_email ? $user->user_email : null,
			'display_name' => $user->display_name ? $user->display_name : null,
			// The single highest-privilege role WordPress reports. The backend
			// maps it through an allow-list, so a role invented by some other
			// plugin cannot become Law Agent staff.
			'wp_role'      => self::primary_role( $user ),
			'site'         => untrailingslashit( home_url() ),
			'iat'          => $now,
			'exp'          => $now + self::TTL,
			// Single use. The backend burns this on a primary key, so a
			// replayed assertion is refused rather than issuing a second
			// token pair.
			'jti'          => bin2hex( random_bytes( 16 ) ),
		);

		$encoded = self::b64url( wp_json_encode( $payload ) );

		// Signed over the ENCODED payload, not the array. Re-serialising JSON
		// on the other side to check a signature is how key ordering and
		// unicode escaping differences between PHP and Node turn into
		// signatures that fail intermittently and inexplicably.
		$signature = hash_hmac( 'sha256', $encoded, $secret, true );

		return new WP_REST_Response(
			array(
				'logged_in'  => true,
				'assertion'  => $encoded . '.' . self::b64url( $signature ),
				'expires_in' => self::TTL,
			),
			200
		);
	}

	/**
	 * One role, highest first.
	 *
	 * WordPress allows several; the backend's mapping is a single value, and
	 * picking the most privileged is the only answer that does not depend on
	 * the order the roles happen to be stored in.
	 */
	private static function primary_role( $user ) {
		$order = array( 'administrator', 'editor', 'author', 'contributor', 'subscriber' );
		$roles = (array) $user->roles;
		foreach ( $order as $role ) {
			if ( in_array( $role, $roles, true ) ) {
				return $role;
			}
		}
		return $roles ? (string) reset( $roles ) : null;
	}
}
