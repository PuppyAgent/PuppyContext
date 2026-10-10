import { useLayoutEffect, useRef } from 'react';

/** Each mounted conversation retains its own reading position, including while
 * hidden. Streaming follows the bottom only while the reader stays there. */
export function useConversationScroll(identity: unknown, active: boolean, revision: string) {
  const viewport = useRef<HTMLDivElement>(null);
  const position = useRef({ identity, top: 0, follow: true });
  const wasActive = useRef(false);
  useLayoutEffect(() => {
    if (position.current.identity !== identity) position.current = { identity, top: 0, follow: true };
    const element = viewport.current;
    if (active && element) {
      if (position.current.follow) element.scrollTop = element.scrollHeight;
      else if (!wasActive.current) element.scrollTop = position.current.top;
    }
    wasActive.current = active;
  }, [identity, active, revision]);
  const onScroll = () => {
    const element = viewport.current;
    if (!active || !element?.clientHeight) return;
    position.current = { identity, top: element.scrollTop,
      follow: element.scrollHeight - element.clientHeight - element.scrollTop < 48 };
  };
  return { viewport, onScroll };
}
