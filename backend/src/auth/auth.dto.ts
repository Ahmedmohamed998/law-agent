/**
 * The wire contract. Identical field names to the Python service's
 * `product/api/schemas.py`, because the frontend should not be able to tell
 * which implementation answered.
 */

import {
  IsEmail,
  IsIn,
  IsOptional,
  IsString,
  Length,
  Matches,
  MaxLength,
} from "class-validator";

export const ROLES = ["owner", "admin", "lawyer", "client"] as const;
export type Role = (typeof ROLES)[number];

export class SignupDto {
  @IsEmail({}, { message: "must be a valid email address" })
  @MaxLength(320)
  email!: string;

  // The floor is length, not composition. Character-class rules push people
  // toward "Password1!" and measurably do not help; length does.
  @IsString()
  @Length(10, 200, { message: "password must be at least 10 characters" })
  password!: string;

  @IsOptional()
  @IsString()
  @MaxLength(160)
  display_name?: string;
}

export class LoginDto {
  @IsEmail({}, { message: "must be a valid email address" })
  @MaxLength(320)
  email!: string;

  @IsString()
  @Length(1, 200)
  password!: string;

  /** Only meaningful for a user who belongs to more than one organization. */
  @IsOptional()
  @IsString()
  @MaxLength(32)
  organization_id?: string;
}

export class RefreshDto {
  @IsString()
  @Length(16, 512)
  refresh_token!: string;

  @IsOptional()
  @IsString()
  @MaxLength(32)
  organization_id?: string;
}

export class WordPressLoginDto {
  /** `<base64url(payload)>.<base64url(hmac)>`, minted by the WordPress plugin. */
  @IsString()
  @Length(32, 4096)
  assertion!: string;
}

export class LogoutDto {
  @IsString()
  @Length(16, 512)
  refresh_token!: string;
}

export class CreateOrgDto {
  @IsString()
  @Length(2, 200)
  name!: string;

  @IsString()
  @Length(2, 80)
  @Matches(/^[a-z0-9][a-z0-9-]*$/, {
    message: "slug must be lowercase letters, digits and hyphens",
  })
  slug!: string;
}

export class AddMemberDto {
  @IsEmail({}, { message: "must be a valid email address" })
  @MaxLength(320)
  email!: string;

  @IsOptional()
  @IsIn(ROLES)
  role?: Role;
}

// ── responses ─────────────────────────────────────────────────────────────

export interface MembershipOut {
  organization_id: string;
  organization_name: string;
  role: string;
}

export interface UserOut {
  user_id: string;
  email: string | null;
  display_name: string | null;
  is_anonymous: boolean;
  email_verified: boolean;
  created_at: Date;
  memberships: MembershipOut[];
}

export interface TokenOut {
  access_token: string;
  token_type: "Bearer";
  expires_in: number;
  refresh_token: string;
  user: UserOut;
}
