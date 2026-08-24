import { Module } from "@nestjs/common";

import { SecurityModule } from "../security/security.module";
import { OrgsController } from "./orgs.controller";
import { OrgsService } from "./orgs.service";

@Module({
  imports: [SecurityModule],
  controllers: [OrgsController],
  providers: [OrgsService],
})
export class OrgsModule {}
