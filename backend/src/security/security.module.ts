import { Module } from "@nestjs/common";

import { KeyService } from "./key.service";
import { PasswordService } from "./password.service";
import { TokenService } from "./token.service";

@Module({
  providers: [KeyService, PasswordService, TokenService],
  exports: [KeyService, PasswordService, TokenService],
})
export class SecurityModule {}
