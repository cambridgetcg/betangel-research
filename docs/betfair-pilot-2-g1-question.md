# G1 follow-up for Pilot 2 (draft for Yu)

Status: draft, not sent. Written by lab-Claude on 2026-11-05 for the [Pilot 2 page](betfair-pilot-2-policy.md). The lab never contacts Betfair: Yu sends it, or decides not to. It contains no account name, ID, key, IP address or secret.

## Note for Yu
- **Why a follow-up.** The first G1 question, drafted for Pilot 1, described only Pilot 1's shape: back the favourite, one bet per race, any unmatched part cancelled at once. Whatever Betfair answered to it, Pilot 2 is a new shape, with resting lays as well as backs, cancelled 60 seconds before the off. So the page's go-live consent needs Betfair's answer to this follow-up, or Yu's recorded decision to go ahead without one.
- **Where to send it.** The Developer Support request form, https://support.developer.betfair.com/hc/en-us/requests/new. If the first question was answered on a ticket, replying on that ticket keeps the context. The message's first line thanks Betfair for that earlier answer, so drop it if there was none.
- **Proposed numbers in the message.** "10 in all" and "ending by 30 June 2027" are proposals until Yu's build consent. If Yu changes either, change the message before sending it.
- **The form may ask for your App Key or account details.** Those go in the form's own fields, at your discretion, never in the message text and never in any repo copy.
- **What counts as an answer** is Yu's to decide. *Lab suggestion:* if Betfair says any part is not allowed, nothing goes live until Yu decides again. A written reply that answers Q1 and Q3 directly counts. A reply that only points to the general read-only article counts as no answer for Q2.
- **Keep the reply off the repo,** on the Air, because it may quote account details. Any repo record holds only the date and the gist.

---

## The message (copy from here)

**Subject:** Follow-up: small automated resting orders on my personal account

Hello,

Thank you for your earlier answer about my small automated betting trial. I am planning a second, separate trial on the same personal account and would like to check it too.

- The same account, used only by me with my own money, and the same live application key.
- Software I commissioned for my own use, written with AI coding assistants. It follows one fixed rule; no AI model makes betting decisions. I start and attend every session.
- In GB and IE horse-racing win markets, about five minutes before the off, when one runner has a gap of three or four price steps between the best back and lay prices, it places one £2 back order and one £2 lay order on that runner. Both are priced inside the gap, so they wait to be matched. The lay price is never above 6.0.
- About 60 seconds before the off it cancels whatever is still unmatched. Anything matched stays to the result. No in-play bets.
- One market at a time, at most 5 markets a day and 10 in all, ending by 30 June 2027.
- Before that, to test its safety stops, it places a few £1 orders at prices that cannot realistically be matched (a lay at 1.01 and a back at 1000), and cancels them.

Questions:

1. Are small automated resting back and lay orders like this, on my own account only, within personal API use and your terms?
2. My delayed key screens the markets first, and the live key reads prices only for a market the software is about to decide on. Some of those reads end with no order. Is that pattern acceptable for the live key?
3. Does my daily loss limit count the liability of an unmatched lay order while it waits?

Thank you for your help.

Kind regards,
[Yu's name]
