import Image from "next/image";
import Link from "next/link";
import type { ReactNode } from "react";
export function AppShell({
  screen,
  count,
  waiting,
  children,
}: {
  screen: string;
  count: number;
  waiting: number;
  children: ReactNode;
}) {
  return (
    <div className="app">
      <aside className="rail">
        <div className="brand">
          <Image src="/logo.svg" width={28} height={28} alt="" unoptimized />
          <span>weaver</span>
        </div>
        <nav className="nav" id="nav">
          {[
            ["fabric", "Fabric", "/"],
            ["spinner", "Spinner", "/spinner"],
            ["loom", "Loom", "/loom"],
          ].map(([key, label, href]) => (
            <Link
              key={key}
              href={href}
              aria-current={screen === key ? "page" : undefined}
              data-s={key}
            >
              <span>{label}</span>
              {key === "loom" ? (
                <span className="badge" title={`검토 대기 ${waiting}건`}>
                  {waiting}
                </span>
              ) : (
                <span className="sub">
                  {key === "fabric" ? `Thread ${count}개` : ""}
                </span>
              )}
            </Link>
          ))}
        </nav>
        <div className="grow" />
      </aside>
      <main id="main">{children}</main>
    </div>
  );
}
