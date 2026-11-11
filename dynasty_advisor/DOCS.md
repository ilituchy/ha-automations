# Dynasty Advisor

Trade targets and add/drop recommendations for a Sleeper dynasty league,
on a page in the Home Assistant sidebar.

1. **Configuration** tab: set your Sleeper username. Set the league ID too if
   you are in more than one dynasty league (it's the long number in the
   league's URL on sleeper.com).
2. Start the app and open **Dynasty Advisor** in the sidebar.
3. Pick a strategy and press **Run now**. A run takes under a minute.

Sleeper's public API is read-only, so the app never changes anything in your
league — every trade and pickup is a suggestion you make yourself in Sleeper.

Each report lists trade ideas (an opening offer and a walk-away ceiling, with
the reasons: the other team's record and playoff position, what they're thin
at, and how often that manager has accepted trades), IR moves and pickups, the
league's contenders and sellers, every manager's trade history and your draft
picks.

Values come from FantasyCalc; trending data from Sleeper. See the README in
the repository for how ideas are scored and the known limits.
