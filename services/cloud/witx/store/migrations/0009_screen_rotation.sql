-- A screenshot's state records the screen's rotation (0, 90, 180 or 270 degrees), not
-- just portrait or landscape: a phone turned round has the same size, and input meant
-- for the other way round must be refused. Old landscape records become 90; an input
-- against one on a phone at 270 is refused, never run.
UPDATE observations
   SET screen = (screen - 'orientation')
                || jsonb_build_object('rotation',
                                      CASE screen->>'orientation' WHEN 'landscape' THEN 90 ELSE 0 END)
 WHERE screen ? 'orientation';
