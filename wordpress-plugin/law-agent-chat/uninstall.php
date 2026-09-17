<?php
/**
 * Removes this plugin's own settings.
 *
 * Nothing else is ours to delete. Conversations live in the AI service and
 * accounts in the product backend; erasing a person is a call to
 * DELETE /v1/users/{id}/data from the backend, never from here.
 */

if ( ! defined( 'WP_UNINSTALL_PLUGIN' ) ) {
	exit;
}

delete_option( 'law_agent_chat_options' );
delete_site_option( 'law_agent_chat_options' );
delete_option( 'law_agent_chat_rewrite' );

// The My Account endpoint goes with the plugin; leaving its rule cached
// would 404 /my-account/consultations/ instead of falling through.
flush_rewrite_rules( false );
