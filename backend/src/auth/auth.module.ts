import { Module } from "@nestjs/common";

import { ErasureModule } from "../erasure/erasure.module";
import { SecurityModule } from "../security/security.module";
import { AuthController } from "./auth.controller";
import { AuthService } from "./auth.service";
import { WordPressService } from "./wordpress.service";

@Module({
  imports: [SecurityModule, ErasureModule],
  controllers: [AuthController],
  providers: [AuthService, WordPressService],
  exports: [AuthService, WordPressService],
})
export class AuthModule {}
