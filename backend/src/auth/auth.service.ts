/**
 * Signup, login, refresh, logout — and the anonymous path that makes the
 * funnel work.
 *
 * The decision worth reading before changing anything: an anonymous user is a
 * **real user row** with a real id, and signup UPGRADES that row in place
 * rather than creating a second one. The `sub` in their token never changes,
 * so the conversations the AI service already stored under that id are still
 * theirs after they register.
 *
 * The alternative — minting a throwaway `anon_7f3c` subject and reassigning
 * sessions at signup — needs a cross-service call, an endpoint on the AI side
 * that does not exist, and a window where a crash strands someone's history
 * under a subject nobody owns. Keeping the id stable deletes that problem
 * instead of solving it.
 */

import { Injectable, Logger } from "@nestjs/common";
import type { Request } from "express";

import { conflict, forbidden, notFound, unauthorized } from "../common/errors";
import { newId } from "../common/ids";
import { PrismaService } from "../prisma/prisma.service";
import { WordPressService, type WordPressClaims } from "./wordpress.service";
import { PasswordService } from "../security/password.service";
import { TokenService } from "../security/token.service";
import type { Caller } from "./auth.guard";
import type { MembershipOut, TokenOut, UserOut } from "./auth.dto";

type UserWithMemberships = {
  id: string;
  email: string | null;
  display_name: string | null;
  is_anonymous: boolean;
  email_verified_at: Date | null;
  password_hash: string | null;
  status: string;
  created_at: Date;
  // Null for anyone who predates the WordPress link, or who never had one.
  wp_user_id: number | null;
  wp_role: string | null;
  memberships: {
    organization_id: string;
    role: string;
    created_at: Date;
    organizations: { name: string };
  }[];
};

const WITH_MEMBERSHIPS = {
  memberships: {
    include: { organizations: { select: { name: true } } },
    orderBy: { created_at: "asc" as const },
  },
};

/**
 * Lowercased and trimmed. The partial unique index enforces exactly this form,
 * so every write path must agree on it or the constraint protects nothing.
 */
export function normalizeEmail(raw: string): string {
  return raw.trim().toLowerCase();
}

@Injectable()
export class AuthService {
  private static readonly log = new Logger(AuthService.name);

  constructor(
    private readonly prisma: PrismaService,
    private readonly passwords: PasswordService,
    private readonly tokens: TokenService,
  ) {}

  // ── shaping ─────────────────────────────────────────────────────────────

  userOut(user: UserWithMemberships): UserOut {
    const memberships: MembershipOut[] = user.memberships.map((m) => ({
      organization_id: m.organization_id,
      organization_name: m.organizations.name,
      role: m.role,
    }));
    return {
      user_id: user.id,
      email: user.email,
      display_name: user.display_name,
      is_anonymous: user.is_anonymous,
      email_verified: user.email_verified_at !== null,
      created_at: user.created_at,
      memberships,
    };
  }

  /**
   * Which (org_id, role) go into the token.
   *
   * A user in no organization gets neither claim — the AI service reads that
   * as an individual and falls back to `user_id` filtering. A user in exactly
   * one gets it implicitly. A user in several must be explicit, and the oldest
   * membership is the default rather than an error, because failing a login
   * over an ambiguity the client did not know existed is a bad first
   * experience.
   */
  private claimContext(
    user: UserWithMemberships,
    requestedOrg?: string,
  ): { organizationId: string | null; role: string | null } {
    const memberships = user.memberships;
    if (memberships.length === 0) {
      // No organization, but a WordPress administrator is still staff. The
      // role travels without an org id, which is what StaffGuard checks --
      // inventing an organization just to carry a role would put a fake row
      // in front of every real one.
      return {
        organizationId: null,
        role: WordPressService.mapRole(user.wp_role),
      };
    }

    if (requestedOrg) {
      const match = memberships.find((m) => m.organization_id === requestedOrg);
      if (!match) throw forbidden("not a member of that organization");
      return { organizationId: match.organization_id, role: match.role };
    }

    const first = memberships[0];
    return { organizationId: first.organization_id, role: first.role };
  }

  /** Mint an access token and a fresh refresh token. */
  private async issue(
    user: UserWithMemberships,
    req: Request,
    requestedOrg?: string,
    familyId?: string,
  ): Promise<TokenOut> {
    const { organizationId, role } = this.claimContext(user, requestedOrg);

    const access = await this.tokens.issueAccessToken({
      userId: user.id,
      organizationId,
      role,
      anonymous: user.is_anonymous,
    });

    const [plaintext, tokenHash] = this.tokens.newRefreshToken();
    await this.prisma.refresh_tokens.create({
      data: {
        id: newId(),
        user_id: user.id,
        // A rotation stays in its family; a fresh login starts a new one. That
        // is what lets a leak revoke one device's chain without logging the
        // user out everywhere.
        family_id: familyId ?? newId(),
        token_hash: tokenHash,
        expires_at: this.tokens.refreshExpiry(),
        user_agent: (req.headers["user-agent"] ?? "").toString().slice(0, 400) || null,
        ip: req.ip ?? null,
      },
    });

    return {
      access_token: access.token,
      token_type: "Bearer",
      expires_in: access.expiresIn,
      refresh_token: plaintext,
      user: this.userOut(user),
    };
  }

  // ── anonymous ───────────────────────────────────────────────────────────

  /**
   * A signed token for someone who has not signed up yet.
   *
   * Deliberately a real token and a real user row, not a bypass. The AI
   * service has one identity mechanism and this goes through it — so anonymous
   * conversations are tenant-scoped, rate-limitable, and erasable like any
   * other, instead of being a special case in every downstream query.
   */
  async anonymous(req: Request): Promise<TokenOut> {
    const user = await this.prisma.users.create({
      data: { id: newId(), is_anonymous: true },
      include: WITH_MEMBERSHIPS,
    });
    return this.issue(user as UserWithMemberships, req);
  }

  // ── signup ──────────────────────────────────────────────────────────────

  async signup(
    email: string,
    password: string,
    displayName: string | undefined,
    req: Request,
    upgradingUserId: string | null,
  ): Promise<TokenOut> {
    const normalized = normalizeEmail(email);
    const passwordHash = await this.passwords.hash(password);

    let user: UserWithMemberships | null = null;

    if (upgradingUserId) {
      const existing = await this.prisma.users.findFirst({
        where: { id: upgradingUserId, deleted_at: null, is_anonymous: true },
        include: WITH_MEMBERSHIPS,
      });
      if (existing) {
        try {
          user = (await this.prisma.users.update({
            where: { id: existing.id },
            data: {
              email: normalized,
              password_hash: passwordHash,
              is_anonymous: false,
              display_name: displayName ?? existing.display_name,
              last_login_at: new Date(),
              updated_at: new Date(),
            },
            include: WITH_MEMBERSHIPS,
          })) as UserWithMemberships;
        } catch (err) {
          throw this.emailConflict(err);
        }
      }
      // A token saying anonymous with a row that is not means it was already
      // upgraded by a concurrent request — fall through to a plain signup.
    }

    if (!user) {
      try {
        user = (await this.prisma.users.create({
          data: {
            id: newId(),
            email: normalized,
            password_hash: passwordHash,
            is_anonymous: false,
            display_name: displayName ?? null,
            last_login_at: new Date(),
          },
          include: WITH_MEMBERSHIPS,
        })) as UserWithMemberships;
      } catch (err) {
        throw this.emailConflict(err);
      }
    }

    return this.issue(user, req);
  }

  /**
   * The partial unique index on email.
   *
   * Same message whichever path hit it — a distinct "already registered" reply
   * turns signup into an oracle for which addresses hold accounts.
   */
  private emailConflict(err: unknown): Error {
    const code = (err as { code?: string }).code;
    if (code === "P2002") {
      return conflict("email_unavailable", "that email cannot be registered");
    }
    return err as Error;
  }

  // ── login ───────────────────────────────────────────────────────────────

  async login(
    email: string,
    password: string,
    requestedOrg: string | undefined,
    req: Request,
  ): Promise<TokenOut> {
    const normalized = normalizeEmail(email);

    const user = (await this.prisma.users.findFirst({
      where: { email: normalized, deleted_at: null, is_anonymous: false },
      include: WITH_MEMBERSHIPS,
    })) as UserWithMemberships | null;

    if (!user) {
      // Spend the same time as a real verification, so the response does not
      // answer "does this address have an account?".
      await this.passwords.dummyVerify();
      throw unauthorized("invalid email or password");
    }

    if (!(await this.passwords.verify(user.password_hash, password))) {
      throw unauthorized("invalid email or password");
    }

    if (user.status !== "active") throw forbidden("account is disabled");

    // The only moment the plaintext is in hand — upgrade a hash made with
    // older parameters rather than asking the user to change their password.
    if (user.password_hash && this.passwords.needsRehash(user.password_hash)) {
      const rehashed = await this.passwords.hash(password);
      await this.prisma.users.update({
        where: { id: user.id },
        data: { password_hash: rehashed },
      });
    }

    await this.prisma.users.update({
      where: { id: user.id },
      data: { last_login_at: new Date() },
    });

    return this.issue(user, req, requestedOrg);
  }

  // ── refresh ─────────────────────────────────────────────────────────────

  /**
   * Rotate the refresh token and mint a new access token.
   *
   * Reuse detection: presenting a token that was already spent means it
   * leaked, because the legitimate client always holds the newest one. At that
   * point the attacker and the user are indistinguishable, so the entire
   * family is revoked and both must log in again.
   *
   * The revocation CANNOT happen inside the transaction that detects it. A
   * transaction rolls back when the handler throws, so revoking and then
   * throwing a 401 would undo the revocation and leave the leaked family alive
   * — a reuse detector that detects and does nothing. This exact bug was found
   * and fixed in the Python implementation; it is not hypothetical.
   */
  async refresh(
    refreshToken: string,
    requestedOrg: string | undefined,
    req: Request,
  ): Promise<TokenOut> {
    const tokenHash = this.tokens.hashRefreshToken(refreshToken);

    const row = await this.prisma.refresh_tokens.findUnique({
      where: { token_hash: tokenHash },
    });
    if (!row) throw unauthorized("invalid refresh token");
    if (row.revoked_at) throw unauthorized("refresh token revoked");

    if (row.used_at) {
      // Committed on its own, before the throw.
      await this.prisma.refresh_tokens.updateMany({
        where: { family_id: row.family_id, revoked_at: null },
        data: { revoked_at: new Date() },
      });
      AuthService.log.warn(`refresh token reuse detected, family=${row.family_id}`);
      throw unauthorized("refresh token reuse detected; sign in again");
    }

    if (row.expires_at <= new Date()) throw unauthorized("refresh token expired");

    // Claim the token with a conditional update: `used_at: null` in the WHERE
    // means two concurrent refreshes cannot both succeed, and the loser is
    // treated as what it is — a second use.
    const claimed = await this.prisma.refresh_tokens.updateMany({
      where: { token_hash: tokenHash, used_at: null, revoked_at: null },
      data: { used_at: new Date() },
    });
    if (claimed.count === 0) {
      throw unauthorized("refresh token already used");
    }

    const user = (await this.prisma.users.findFirst({
      where: { id: row.user_id, deleted_at: null },
      include: WITH_MEMBERSHIPS,
    })) as UserWithMemberships | null;

    if (!user || user.status !== "active") {
      throw unauthorized("account is no longer active");
    }

    return this.issue(user, req, requestedOrg, row.family_id);
  }

  // ── logout ──────────────────────────────────────────────────────────────

  /**
   * Revoke the whole family this token belongs to.
   *
   * Note what this cannot do: the access token stays valid until it expires.
   * There is no denylist, which is exactly why its TTL is minutes.
   */
  async logout(refreshToken: string): Promise<void> {
    const tokenHash = this.tokens.hashRefreshToken(refreshToken);
    const row = await this.prisma.refresh_tokens.findUnique({
      where: { token_hash: tokenHash },
    });
    if (!row) return; // already gone; 204 either way, nothing to disclose
    await this.prisma.refresh_tokens.updateMany({
      where: { family_id: row.family_id, revoked_at: null },
      data: { revoked_at: new Date() },
    });
  }

  // ── profile ─────────────────────────────────────────────────────────────

  async me(caller: Caller): Promise<UserOut> {
    const user = (await this.prisma.users.findFirst({
      where: { id: caller.userId, deleted_at: null },
      include: WITH_MEMBERSHIPS,
    })) as UserWithMemberships | null;

    if (!user) throw notFound("user_not_found", "no such user");
    if (user.status !== "active") throw forbidden("account is disabled");
    return this.userOut(user);
  }

  // ── WordPress ───────────────────────────────────────────────────────────

  /**
   * Exchange a verified WordPress assertion for our own tokens.
   *
   * Three ways a row is found, in order, and the order is the whole design:
   *
   *   1. Already linked by wp_user_id — the normal repeat login.
   *   2. The caller's own anonymous row — they chatted, then logged in. The
   *      id is kept, so the conversations they already had are already
   *      theirs. Same trick as signup, for the same reason.
   *   3. An existing account with the same email — someone who registered
   *      through the widget before WordPress owned identity. Linked rather
   *      than duplicated, so their history and consultations survive.
   *
   * Only then is a new row created.
   */
  async fromWordPress(
    claims: WordPressClaims,
    req: Request,
    callerUserId: string | null,
  ): Promise<TokenOut> {
    const email = claims.email ? normalizeEmail(claims.email) : null;

    let user = (await this.prisma.users.findFirst({
      where: { wp_user_id: claims.wp_user_id, deleted_at: null },
      include: WITH_MEMBERSHIPS,
    })) as UserWithMemberships | null;

    if (!user && callerUserId) {
      user = (await this.prisma.users.findFirst({
        where: { id: callerUserId, deleted_at: null, is_anonymous: true },
        include: WITH_MEMBERSHIPS,
      })) as UserWithMemberships | null;
    }

    if (!user && email) {
      user = (await this.prisma.users.findFirst({
        where: { email, deleted_at: null, wp_user_id: null },
        include: WITH_MEMBERSHIPS,
      })) as UserWithMemberships | null;
    }

    const shared = {
      wp_user_id: claims.wp_user_id,
      wp_role: claims.wp_role ?? null,
      is_anonymous: false,
      display_name: claims.display_name ?? user?.display_name ?? null,
      last_login_at: new Date(),
      updated_at: new Date(),
    };

    try {
      if (user) {
        user = (await this.prisma.users.update({
          where: { id: user.id },
          // The email follows WordPress, which is now the authority for it.
          data: { ...shared, email: email ?? user.email },
          include: WITH_MEMBERSHIPS,
        })) as UserWithMemberships;
      } else {
        user = (await this.prisma.users.create({
          data: { id: newId(), email, ...shared },
          include: WITH_MEMBERSHIPS,
        })) as UserWithMemberships;
      }
    } catch (err) {
      // Two concurrent first logins race on uq_users_wp_user_id. The loser
      // reads the row the winner wrote rather than failing a login over it.
      if ((err as { code?: string }).code === "P2002") {
        user = (await this.prisma.users.findFirst({
          where: { wp_user_id: claims.wp_user_id, deleted_at: null },
          include: WITH_MEMBERSHIPS,
        })) as UserWithMemberships | null;
        if (!user) throw err;
      } else {
        throw err;
      }
    }

    return this.issue(user, req);
  }

}
