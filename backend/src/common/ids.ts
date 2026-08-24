/**
 * Identifiers, byte-compatible with what the Python service wrote.
 *
 * `public.users.id` becomes `ai.sessions.user_id` on the other side, so the
 * format is part of the cross-service contract: 48 bits of millisecond
 * timestamp + 80 bits of randomness, Crockford base32, 26 characters. That is
 * the ULID spec, and Python's `product/db/ids.py` implements it by hand —
 * verified to produce the same alphabet and length as this package.
 *
 * Sorting by id sorts by creation time, which keeps B-tree inserts local and
 * makes an id a useful debugging breadcrumb.
 */

import { ulid } from "ulid";

export function newId(): string {
  return ulid();
}
