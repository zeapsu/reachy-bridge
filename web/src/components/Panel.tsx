import type { ReactNode } from "react";
import { useLens } from "../glass.ts";

interface Props {
  title: string;
  titleId: string;
  id?: string;
  className?: string;
  /** Beside the title: a status chip, a button. */
  aside?: ReactNode;
  /** Inside another sheet (System's): a titled group, not a sheet of its own. */
  inset?: boolean;
  children: ReactNode;
}

/** A titled panel: its own glass sheet, or a group inside one. */
export function Panel({ title, titleId, id, className, aside, inset, children }: Props) {
  const sheet = useLens<HTMLElement>();
  const frame = inset ? "group panel-inset" : "glass sheet";
  return (
    <section
      className={className ? `${frame} ${className}` : frame}
      data-lens={inset ? undefined : ""}
      ref={inset ? undefined : sheet}
      id={id}
      aria-labelledby={titleId}
    >
      <header className={inset ? "group-head" : undefined}>
        <h2 id={titleId}>{title}</h2>
        {aside}
      </header>
      {children}
    </section>
  );
}
