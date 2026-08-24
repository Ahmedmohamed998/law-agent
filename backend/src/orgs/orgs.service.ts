/**
 * Organizations and membership.
 *
 * This is what fills the `org_id` claim, which the AI service turns into a
 * row-level-security tenant key. So a bug here is not a product bug — it is a
 * data-isolation bug on the other side of the boundary, where one firm's
 * employment disputes become visible to another. Every write path is
 * authorization-checked explicitly.
 */

import { Injectable } from "@nestjs/common";

import { conflict, forbidden, notFound } from "../common/errors";
import { newId } from "../common/ids";
import { PrismaService } from "../prisma/prisma.service";
import { normalizeEmail } from "../auth/auth.service";
import type { Role } from "../auth/auth.dto";

// Who may change membership. `lawyer` is deliberately absent: practising in an
// organization is not the same authority as deciding who belongs to it.
const MANAGING_ROLES = new Set(["owner", "admin"]);

export interface OrgOut {
  organization_id: string;
  name: string;
  slug: string;
  created_at: Date;
  my_role: string | null;
}

export interface MemberOut {
  user_id: string;
  email: string | null;
  display_name: string | null;
  role: string;
  joined_at: Date;
}

@Injectable()
export class OrgsService {
  constructor(private readonly prisma: PrismaService) {}

  /**
   * The caller's membership, or a 404.
   *
   * Same 404 whether the organization does not exist or the caller is not in
   * it — otherwise this endpoint enumerates other firms' organization ids.
   */
  private async membership(userId: string, orgId: string) {
    const row = await this.prisma.memberships.findUnique({
      where: { user_id_organization_id: { user_id: userId, organization_id: orgId } },
    });
    if (!row) throw notFound("organization_not_found", "no such organization");
    return row;
  }

  private async requireManager(userId: string, orgId: string) {
    const m = await this.membership(userId, orgId);
    if (!MANAGING_ROLES.has(m.role)) throw forbidden("requires owner or admin");
    return m;
  }

  /** Create an organization. The caller becomes its owner. */
  async create(userId: string, name: string, slug: string): Promise<OrgOut> {
    const existing = await this.prisma.organizations.findUnique({ where: { slug } });
    if (existing) throw conflict("slug_taken", "that slug is in use");

    const org = await this.prisma.organizations.create({
      data: {
        id: newId(),
        name,
        slug,
        memberships: { create: { user_id: userId, role: "owner" } },
      },
    });

    return {
      organization_id: org.id,
      name: org.name,
      slug: org.slug,
      created_at: org.created_at,
      my_role: "owner",
    };
  }

  async listMine(userId: string): Promise<OrgOut[]> {
    const rows = await this.prisma.memberships.findMany({
      where: { user_id: userId, organizations: { deleted_at: null } },
      include: { organizations: true },
      orderBy: { created_at: "asc" },
    });
    return rows.map((m) => ({
      organization_id: m.organization_id,
      name: m.organizations.name,
      slug: m.organizations.slug,
      created_at: m.organizations.created_at,
      my_role: m.role,
    }));
  }

  async members(callerId: string, orgId: string): Promise<MemberOut[]> {
    await this.membership(callerId, orgId); // authorises, 404s if not ours
    const rows = await this.prisma.memberships.findMany({
      where: { organization_id: orgId },
      include: { users: true },
      orderBy: { created_at: "asc" },
    });
    return rows.map((m) => ({
      user_id: m.user_id,
      email: m.users.email,
      display_name: m.users.display_name,
      role: m.role,
      joined_at: m.created_at,
    }));
  }

  /**
   * Add an existing user to the organization by email.
   *
   * Invitation email flows are not built; this assumes the person already has
   * an account. Creating a user here would mean minting an account someone
   * never asked for.
   */
  async addMember(
    callerId: string,
    orgId: string,
    email: string,
    role: Role,
  ): Promise<MemberOut> {
    await this.requireManager(callerId, orgId);

    const user = await this.prisma.users.findFirst({
      where: { email: normalizeEmail(email), deleted_at: null, is_anonymous: false },
    });
    if (!user) throw notFound("user_not_found", "no registered user with that email");

    const row = await this.prisma.memberships.upsert({
      where: {
        user_id_organization_id: { user_id: user.id, organization_id: orgId },
      },
      create: { user_id: user.id, organization_id: orgId, role },
      update: { role },
    });

    return {
      user_id: user.id,
      email: user.email,
      display_name: user.display_name,
      role: row.role,
      joined_at: row.created_at,
    };
  }

  async removeMember(callerId: string, orgId: string, userId: string): Promise<void> {
    await this.requireManager(callerId, orgId);
    const target = await this.membership(userId, orgId);

    if (target.role === "owner") {
      const otherOwners = await this.prisma.memberships.count({
        where: { organization_id: orgId, role: "owner", user_id: { not: userId } },
      });
      if (otherOwners === 0) {
        // An organization with no owner cannot be administered again without a
        // database edit.
        throw forbidden("cannot remove the last owner");
      }
    }

    await this.prisma.memberships.delete({
      where: { user_id_organization_id: { user_id: userId, organization_id: orgId } },
    });
  }
}
