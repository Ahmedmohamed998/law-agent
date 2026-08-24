import { Module } from "@nestjs/common";

import { AiModule } from "../ai/ai.module";
import { SecurityModule } from "../security/security.module";
import { ErasureAdminController } from "./erasure.controller";
import { ErasureService } from "./erasure.service";

@Module({
  imports: [AiModule, SecurityModule],
  controllers: [ErasureAdminController],
  providers: [ErasureService],
  exports: [ErasureService],
})
export class ErasureModule {}
