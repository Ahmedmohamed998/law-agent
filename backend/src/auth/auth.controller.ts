import {
  Body,
  Controller,
  Delete,
  Get,
  HttpCode,
  Post,
  Req,
  UseGuards,
} from "@nestjs/common";
import type { Request } from "express";

import {
  LoginDto,
  LogoutDto,
  RefreshDto,
  SignupDto,
  WordPressLoginDto,
  type TokenOut,
  type UserOut,
} from "./auth.dto";
import {
  AuthGuard,
  CurrentCaller,
  RegisteredGuard,
  bearerFrom,
  callerFromToken,
  type Caller,
} from "./auth.guard";
import { AuthService } from "./auth.service";
import { WordPressService } from "./wordpress.service";
import { ErasureService, type ErasureOut } from "../erasure/erasure.service";
import { TokenService } from "../security/token.service";

@Controller("auth")
export class AuthController {
  constructor(
    private readonly auth: AuthService,
    private readonly tokens: TokenService,
    private readonly erasure: ErasureService,
    private readonly wordpress: WordPressService,
  ) {}

  @Post("anonymous")
  @HttpCode(201)
  anonymous(@Req() req: Request): Promise<TokenOut> {
    return this.auth.anonymous(req);
  }

  /**
   * Register. If called with an anonymous token, upgrades that user in place.
   *
   * The token is read here rather than through AuthGuard because it is
   * OPTIONAL: signup must work for a first-time visitor with no token at all.
   * An expired or invalid anonymous token is not a reason to refuse a signup —
   * it just means the history cannot be carried over.
   */
  @Post("signup")
  @HttpCode(201)
  async signup(@Body() body: SignupDto, @Req() req: Request): Promise<TokenOut> {
    let upgrading: string | null = null;
    const token = bearerFrom(req);
    if (token) {
      try {
        const caller = await callerFromToken(this.tokens, token);
        if (caller.anonymous) upgrading = caller.userId;
      } catch {
        upgrading = null;
      }
    }
    return this.auth.signup(
      body.email,
      body.password,
      body.display_name,
      req,
      upgrading,
    );
  }

  @Post("login")
  @HttpCode(200)
  login(@Body() body: LoginDto, @Req() req: Request): Promise<TokenOut> {
    return this.auth.login(body.email, body.password, body.organization_id, req);
  }

  @Post("refresh")
  @HttpCode(200)
  refresh(@Body() body: RefreshDto, @Req() req: Request): Promise<TokenOut> {
    return this.auth.refresh(body.refresh_token, body.organization_id, req);
  }

  @Post("logout")
  @HttpCode(204)
  logout(@Body() body: LogoutDto): Promise<void> {
    return this.auth.logout(body.refresh_token);
  }


  /**
   * Log in with a WordPress assertion.
   *
   * WordPress owns the password, the reset flow and email verification --
   * none of which this service has. It does not own tokens: it proves who is
   * logged in, and this endpoint exchanges that proof for a normal RS256
   * token. The AI service is unaware any of it happened.
   *
   * The bearer is OPTIONAL and read the same way signup reads it: if the
   * caller is holding an anonymous token, that same user row is linked, so
   * the conversation they had before logging in stays theirs.
   */
  @Post("wordpress")
  @HttpCode(200)
  async wordpressLogin(@Body() body: WordPressLoginDto, @Req() req: Request): Promise<TokenOut> {
    const claims = await this.wordpress.verify(body.assertion);

    let callerUserId: string | null = null;
    const token = bearerFrom(req);
    if (token) {
      try {
        const caller = await callerFromToken(this.tokens, token);
        if (caller.anonymous) callerUserId = caller.userId;
      } catch {
        // An expired anonymous token is not a reason to refuse a login. It
        // only means the earlier conversation cannot be carried over.
        callerUserId = null;
      }
    }

    return this.auth.fromWordPress(claims, req, callerUserId);
  }

  @Get("me")
  @UseGuards(AuthGuard)
  me(@CurrentCaller() caller: Caller): Promise<UserOut> {
    return this.auth.me(caller);
  }

  /**
   * Decode a token this service issued, and say why if it will not verify.
   *
   * Here for whoever is wiring up the AI service or the frontend: a 401 with
   * no detail is a bad afternoon, and this turns it into one request.
   */
  @Post("introspect")
  @HttpCode(200)
  async introspect(@Req() req: Request) {
    const token = bearerFrom(req);
    if (!token) return { valid: false, error: "missing bearer token" };
    try {
      const claims = await this.tokens.verifyOwnToken(token);
      return { valid: true, claims };
    } catch (err) {
      return {
        valid: false,
        error: `${(err as Error).name}: ${(err as Error).message}`.slice(0, 300),
      };
    }
  }

  /**
   * The user erases themselves.
   *
   * 202, not 204: the local delete is done, but the AI service still has to be
   * told and that delivery is recorded rather than assumed. Poll
   * GET /admin/erasure to see whether it landed.
   */
  @Delete("me")
  @HttpCode(202)
  @UseGuards(AuthGuard, RegisteredGuard)
  eraseMe(@CurrentCaller() caller: Caller): Promise<ErasureOut> {
    return this.erasure.purgeUser(caller.userId);
  }
}
