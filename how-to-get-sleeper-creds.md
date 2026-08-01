after creating league test the api:
export SLEEPER_USERNAME='your_username'

curl -s \
  "https://api.sleeper.app/v1/user/${SLEEPER_USERNAME}" \
  | jq
copy the returned user_id:
export SLEEPER_USER_ID='returned_user_id'

curl -s \
  "https://api.sleeper.app/v1/user/${SLEEPER_USER_ID}/leagues/nfl/2026" \
  | jq '.[] | {
      name,
      league_id,
      status,
      draft_id,
      total_rosters,
      roster_positions
    }'
put the appropriate league_id in .env
