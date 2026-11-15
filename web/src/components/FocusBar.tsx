import type { Thread } from "../lib/api";
import { mdhm } from "../lib/presentation";
export function FocusBar({
  thread,
  previous,
  onExit,
}: {
  thread: Thread | null;
  previous: number;
  onExit: () => void;
}) {
  return (
    <div className="fbar" id="fbar" hidden={!thread}>
      {thread && (
        <>
          <span>
            <b>
              {mdhm(thread.occurred_at)} {thread.kind}
            </b>
            부터 보는 중 (이전 {previous}개 포함)
          </span>
          <button type="button" id="fexit" onClick={onExit}>
            전체 보기
          </button>
        </>
      )}
    </div>
  );
}
