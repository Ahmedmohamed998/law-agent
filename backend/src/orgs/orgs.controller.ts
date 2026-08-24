import {
  Body,
  Controller,
  Delete,
  Get,
  HttpCode,
  Param,
  Post,
  UseGuards,
} from "@nestjs/common";

import { AddMemberDto, CreateOrgDto } from "../auth/auth.dto";
import { AuthGuard, CurrentCaller, RegisteredGuard, type Caller } from "../auth/auth.guard";
import { OrgsService, type MemberOut, type OrgOut } from "./orgs.service";

@Controller("orgs")
@UseGuards(AuthGuard)
export class OrgsController {
  constructor(private readonly orgs: OrgsService) {}

  @Post()
  @HttpCode(201)
  @UseGuards(RegisteredGuard)
  create(@CurrentCaller() caller: Caller, @Body() body: CreateOrgDto): Promise<OrgOut> {
    return this.orgs.create(caller.userId, body.name, body.slug);
  }

  @Get()
  listMine(@CurrentCaller() caller: Caller): Promise<OrgOut[]> {
    return this.orgs.listMine(caller.userId);
  }

  @Get(":orgId/members")
  members(
    @CurrentCaller() caller: Caller,
    @Param("orgId") orgId: string,
  ): Promise<MemberOut[]> {
    return this.orgs.members(caller.userId, orgId);
  }

  @Post(":orgId/members")
  @HttpCode(201)
  @UseGuards(RegisteredGuard)
  addMember(
    @CurrentCaller() caller: Caller,
    @Param("orgId") orgId: string,
    @Body() body: AddMemberDto,
  ): Promise<MemberOut> {
    return this.orgs.addMember(caller.userId, orgId, body.email, body.role ?? "client");
  }

  @Delete(":orgId/members/:userId")
  @HttpCode(204)
  @UseGuards(RegisteredGuard)
  removeMember(
    @CurrentCaller() caller: Caller,
    @Param("orgId") orgId: string,
    @Param("userId") userId: string,
  ): Promise<void> {
    return this.orgs.removeMember(caller.userId, orgId, userId);
  }
}
