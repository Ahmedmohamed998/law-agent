"""
Voice: speech in, speech out.

Speech in turns a recording into text, and the text then goes through the
ordinary message path — same retrieval, same allowance, same idempotency. A
spoken question is not a second kind of turn, only a second way to type one.

Speech out reads a finished answer aloud. It never generates anything new, so
it cannot say something the written answer does not.
"""
