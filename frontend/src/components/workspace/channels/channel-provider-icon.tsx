"use client";

import { MessageCircleIcon } from "lucide-react";
import type { SVGProps } from "react";

import { cn } from "@/lib/utils";

type ChannelProviderIconProps = SVGProps<SVGSVGElement> & {
  provider: string;
};

export function ChannelProviderIcon({
  className,
  ...props
}: ChannelProviderIconProps) {
  return (
    <MessageCircleIcon
      aria-hidden="true"
      className={cn("size-5", className)}
      {...props}
    />
  );
}
