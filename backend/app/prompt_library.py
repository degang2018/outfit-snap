"""Prompt library for the built-in "15-shot outfit-change" template.

This is the exact prompt set the CLI pipeline (gen_viral_gray.py) used to produce
viral_gray_24s.mp4 - promoted to editable template data so the web UI can re-use and
re-tune it.  Prompt heads are picked per shot:

* the change shot uses the full header (it starts in the black "before" outfit and must
  never mention the second outfit as something it is not);
* every shot after the change uses a header that never mentions the black outfit at all -
  otherwise the model pulls the black garment back into frame;
* shots that reveal her back/profile additionally describe the SECOND reference image
  (grey_back.png) so the back of the set matches.
"""
from __future__ import annotations

HEADER = """subject_definitions:
<Subject 1> is the young East Asian female fashion model shown in the FIRST reference image, a five-panel lookbook sheet of the same woman photographed from several angles (full body front, half body front, three-quarter side, full-length back view, seated pose). She has black hair in a loose slightly tousled low bun with soft face-framing strands and very natural light makeup. Her face shape, eyes, eyebrows, nose, lips and hair must match the reference exactly in every shot. IMPORTANT: that sheet is ONE single woman repeated in several poses - it is one person, not several people, and in this video only this one woman is ever present.
<Garment A> is her "before" outfit: a plain black spaghetti-strap camisole and matching black fitted shorts, feet bare. Plain matte black fabric, no print, no logo, no decoration.
<Garment B> is her "after" outfit, the three-piece loungewear set worn in every panel of the reference sheet: a light grey modal camisole with delicate lace trim across the neckline and thin spaghetti straps, a matching light grey three-quarter-sleeve open cardigan with fine ribbing on the sleeves, and matching light grey high-waisted modal wide-leg trousers with a wide ribbed waistband, a gathered ruffled trim at the waist and a front drawstring tie. Its exact light grey colour, the lace shape, the thin straps, the rib texture, the waistband and the soft drape must all be copied from the reference.
<Scene 1> is a clean pure white seamless studio - white backdrop and white floor, photographed with soft cold-white even lighting, no strong shadows and no colour cast.

summary:
[reference generation] A fast, minimal, high-end AI fashion lookbook video of <Subject 1> in <Scene 1>, cutting between <Garment A> and <Garment B> with hard cuts. Clean, bright, modern short-video look.
"""

RETENTION = """
retention_analysis:
<Subject 1>: fully_preserved - EXACT identity from the reference sheet in every shot: same face, same eyebrows, same lips, same loose low bun. Natural skin tone, normal exposure, never washed out, never plastic or doll-like.
<Garment A>: fully_preserved - plain black camisole and black shorts, nothing added.
<Garment B>: fully_preserved - the lace trim, thin camisole straps, the ribbed cardigan sleeves, the wide ribbed waistband with its ruffled trim and drawstring, the wide-leg drape and the trouser length stay exactly as in the reference. The garment never restyles, morphs or gains new pockets, buttons or decoration.
<Scene 1>: fully_preserved - one clean uniform pure white studio, same lighting and white balance in every shot.
"""

RETENTION_B = """
retention_analysis:
<Subject 1>: fully_preserved - EXACT identity from the reference image: same face, same eyebrows, same lips, same loose low bun. Natural skin tone, normal exposure, never washed out, never plastic or doll-like.
<Garment B>: fully_preserved and taken straight from the reference image - it is the same light grey three-piece set the woman in the reference image is wearing: same light grey tone, same lace trim, same thin camisole straps, same ribbing on the cardigan sleeves, same wide ribbed waistband with its ruffled trim and front drawstring, same wide-leg drape and trouser length. The outfit never restyles, morphs, changes colour or gains new pockets, buttons or decoration.
<Scene 1>: fully_preserved - one clean uniform pure white studio, same lighting and white balance in every shot.
"""

BACK_NOTE = """<Subject 1 back view> - <Subject 1> is also shown from behind in the SECOND reference image, the same woman photographed from the back in the same outfit. Her back must match that second reference image exactly: the light grey open cardigan seen from behind (plain light grey back panels, slightly loose, the hem falling past the hip, fine ribbing on the three-quarter sleeves), the same light grey wide-leg trousers with the same ribbed waistband, and her hair in the same low bun. Whenever the camera sees her from behind or in profile, the back of the garment must look exactly like the SECOND reference image - no print, no logo, no new decoration and no different cut.
"""

BACK_RETENTION = "<Subject 1 back view>: fully_preserved from the SECOND reference image whenever the camera sees her from behind or in profile."

HEADER_B = """subject_definitions:
<Subject 1> is the young East Asian female fashion model shown in the FIRST reference image, a five-panel lookbook sheet of the same woman photographed from several angles (full body front, half body front, three-quarter side, full-length back view, seated pose). She has black hair in a loose slightly tousled low bun with soft face-framing strands and very natural light makeup. Her face shape, eyes, eyebrows, nose, lips and hair must match the reference exactly in every shot. IMPORTANT: that sheet is ONE single woman repeated in several poses - it is one person, not several people, and in this video only this one woman is ever present.
<Garment B> is her outfit in this shot, and it is copied directly from the woman in the FIRST reference image: the exact light grey loungewear set she is wearing in that image - a light grey modal camisole with delicate lace trim across the neckline and thin spaghetti straps, a matching light grey three-quarter-sleeve open cardigan with fine ribbing on the sleeves, and matching light grey high-waisted modal wide-leg trousers with a wide ribbed waistband, a gathered ruffled trim at the waist and a front drawstring tie. Copy it from the reference image exactly: same light grey tone, same lace shape, same thin straps, same ribbing, same waistband, same drawstring and same trouser length. She wears this identical outfit in this shot.
<Scene 1> is a clean pure white seamless studio - white backdrop and white floor, photographed with soft cold-white even lighting, no strong shadows and no colour cast.

summary:
[reference generation] A fast, minimal, high-end AI fashion lookbook video of <Subject 1> in <Scene 1>, wearing the same light grey set as the reference image. Clean, bright, modern short-video look.
"""

FOOTER = """
overall_soundscape:
Quiet studio room tone.

non_diegetic_music:
None."""

TAIL = (" CAMERA: locked off on a tripod - absolutely no camera movement, no zoom, no pan, no tilt, no handheld "
        "shake, the framing never changes. MOVEMENT SPEED: everything happens at normal real-time human speed, "
        "never slow motion and never a slow-motion replay. Tone: soft cold-white even studio light, no harsh "
        "shadows, clean minimal high-end look, no film grain, no colour cast. Her face stays natural and calm "
        "with only subtle lifelike micro-expressions. Photorealistic skin and fabric. No text, no subtitles, "
        "no watermark, no logo, no brand name."
        " ONE PERSON ONLY: exactly one single woman - <Subject 1> - appears in the frame for the whole shot. "
        "There is never a second person, never a duplicate or double of her, never a second body in the "
        "background, never a ghost or semi-transparent second figure, never her reflection or mirrored image, "
        "and never a shadow that reads as another person. When the shot mentions an assistant, only one single "
        "hand and forearm of that assistant may enter the frame - no second face, no second body.")

LOCK_B = (" OUTFIT LOCK - THE MOST IMPORTANT RULE OF THIS SHOT: her clothing is exactly the light grey set worn by "
          "the woman in the reference image, and she is wearing it from the very first frame to the very last frame. "
          "She never changes clothes at any point in this shot - no garment flies through the air, nothing morphs, "
          "fades, dissolves or transforms, and the outfit is fully on and fully opaque from frame one.")

LOCK_CHANGE = (" CLOTHING LOCK - THE MOST IMPORTANT RULE OF THIS SHOT: she starts in <Garment A> (black camisole and "
               "black shorts, barefoot) and she ends the shot wearing the complete <Garment B>. The outfit changes "
               "exactly once, at the moment the floating grey set drifts down onto her: as the garment slides down "
               "over her body the clothing is exchanged smoothly and completely in one continuous motion, and once "
               "the fabric has passed her, the new outfit is fully on, clean, opaque and sharp. There is no light "
               "flash, no glow, no sparkles and no other effect; no dissolve, no fading and no see-through ghosting "
               "of one outfit over the other. The clothing never changes at any other moment and the black outfit "
               "never returns."
               " LIMB LOCK: she has exactly two arms and two hands at every single moment of this shot, and two "
               "legs. No extra hand, no extra arm, no extra limb, no third or fourth hand and no disembodied hand "
               "ever appears, in the air or on the clothing. The grey set is only cloth - it never grows its own "
               "arms, hands, sleeves-as-limbs or body parts, and any empty sleeve just hangs limp and downward. "
               "Whenever a sleeve is on screen it is filled by one of her own two arms.")

CHANGE_KEY = "v01"
BACK_KEYS = ("v03", "v04", "v10", "v15")

# key -> (duration_frames, title, options, action)
SHOTS: dict[str, tuple[int, str, dict, str]] = {
    "v01": (84, "抛衣变装:黑套装 → 灰套装", {"source_frames": 120},
            "Wide full-body fixed shot on the pure white studio floor. She is barefoot in <Garment A> (black "
            "camisole and black shorts, arms down), standing straight in the centre facing the camera, holding "
            "the neatly folded grey set in front of her chest with both hands. She then swings both arms and "
            "throws the folded grey set upward herself with her own two hands - a clear, confident, deliberate "
            "throwing motion of her own arms - and the set sails up through the air, unfolds and hovers in the "
            "empty air just above her head. The garment never rises by itself and there is no invisible force: "
            "the upward motion comes only from her own throw, and her hands are the last thing to touch it as "
            "it leaves her. Then the grey set drifts straight down towards her, falling vertically and softly "
            "like weightless cloth. As it descends she reaches both arms straight up above her head to receive "
            "it: her two hands slide into the cardigan sleeves and her arms stay inside those sleeves, the "
            "camisole and the trousers slide down over her head and settle onto her body, and the cardigan "
            "comes to rest on her shoulders with her own arms already inside its sleeves. She then lowers her "
            "arms to her sides, fully dressed in the complete <Garment B>, and stands in a clean upright pose "
            "facing the lens. Her two raised arms are occupied by the garment during the whole descent, so no "
            "sleeve is ever empty and hanging like an arm of its own. The garment wraps around her in one "
            "single continuous fluid motion: no stutter, no jump, no clipping, no glitching, and the fabric "
            "never passes through or intersects her body - it always stays outside her silhouette, draping "
            "onto her cleanly and smoothly."),
    "v02": (40, "近景:化妆师补妆", {},
            "Close-up fixed shot. She is now fully dressed in <Garment B>. Only the arm and hand of a makeup "
            "artist enter the frame from the right holding a powder puff, gently patting the side of her face to "
            "touch up her makeup. She keeps still and calm, her head tilting very slightly. Only the helper's "
            "hand and forearm are visible, no other person appears in frame."),
    "v03": (32, "中景:摄影师入镜 + 转身展示背面", {},
            "Medium fixed shot. She wears the complete <Garment B> with black pointed high-heeled shoes, a small "
            "black shoulder bag on her shoulder and a takeaway coffee cup in one hand. Only the arm of a "
            "photographer enters from the left holding a camera pointed at her. She stands with one leg forward "
            "in a side stance and poses confidently to the lens; she first turns to show the back of the set, "
            "then turns back to the front and lightly smooths the fabric with one hand."),
    "v04": (40, "中景:背面展示套装", {},
            "Medium fixed shot. She wears the complete <Garment B>. She turns her body to show the back of the "
            "set to the camera, holds the back view for a moment so the back of the cardigan and the waistband "
            "are clearly seen, then turns back to face the lens and lightly smooths the cardigan with one hand."),
    "v05": (32, "近景:补妆", {},
            "Close-up fixed shot. She wears <Garment B>. Only the makeup artist's arm and hand holding a powder "
            "puff enter the frame and pat the side of her face, touching up her makeup. She stays still."),
    "v06": (32, "中近景:比 V 微笑", {},
            "Medium close-up fixed shot at her own eye level, framed from the waist up, the camera standing "
            "upright. She stands facing the lens, leans her upper body slightly toward the camera, makes a V "
            "sign with her fingers beside her face and smiles naturally. She remains standing upright on her "
            "own two feet the whole time - she never sits down, kneels, crouches, bends to the floor or lies "
            "down, and the floor is never shown. She wears <Garment B>."),
    "v07": (28, "全景:整理衣服站定", {},
            "Wide full-body fixed shot. She stands facing the camera in the complete <Garment B>, smooths the "
            "cardigan and the waistband with both hands and settles into a clean, elegant upright pose."),
    "v08": (40, "全景:抬腿 + 报纸姿态", {},
            "Wide full-body fixed shot. She is wearing the complete <Garment B>; she stands with one leg bent "
            "and lifted off the floor, holding an English newspaper in one hand and carrying a small black "
            "shoulder bag, posing confidently to the camera. The long cardigan and the wide-leg trousers move "
            "naturally with her pose."),
    "v09": (40, "近景:口红 + 配饰", {},
            "Close-up fixed shot. She holds a slim silver lipstick and applies it to her lips, calm and natural. "
            "In the same shot her accessories are clearly visible: black-framed sunglasses, a thin sparkling "
            "necklace, a small ring and an ear cuff. She wears <Garment B>."),
    "v10": (28, "全景:侧身站定", {},
            "Wide full-body fixed shot. She stands sideways to the camera holding the newspaper, adjusting her "
            "stance and settling into an elegant pose. She wears <Garment B>."),
    "v11": (32, "中景:接住飞来的黑包", {},
            "Medium fixed shot. She reaches up and catches a black shoulder bag that flies in toward her, slings "
            "it onto her shoulder and settles into a confident pose. She wears the complete <Garment B>."),
    "v12": (32, "中景:摄影师举机 + 咖啡", {},
            "Medium fixed shot. A photographer's arm raises a camera and points it at her; she holds a takeaway "
            "coffee cup and poses toward the lens with a light natural expression. She wears <Garment B>."),
    "v13": (28, "中近景:补口红(上半身)", {},
            "Medium close-up fixed shot framed from the waist up, so her whole upper body is clearly visible: the "
            "grey lace-trim camisole and the open grey cardigan over it are fully in frame, against the empty "
            "pure white background. She holds the slim silver lipstick and touches up her lips, calm and "
            "natural. Only one person is in the frame - no second figure behind her, no reflection and no other "
            "body; the background around her is clean empty white. She wears <Garment B>."),
    "v14": (28, "中景:手机自拍", {},
            "Medium fixed shot. She raises a white smartphone in front of her and pretends to take a mirror "
            "selfie, looking at the phone screen with a light smile. She wears <Garment B>."),
    "v15": (53, "全景:收尾定格", {},
            "Wide full-body fixed shot. She stands sideways holding the newspaper in a beautiful final pose and "
            "holds completely still for the last second as the closing freeze. She wears <Garment B>."),
}

ORDER = ["v01", "v02", "v03", "v04", "v05", "v06", "v07", "v08",
         "v09", "v10", "v11", "v12", "v13", "v14", "v15"]


def compose(key: str) -> str:
    """Full H3 prompt for a shot, in the official reference structure."""
    _, _, _, action = SHOTS[key]
    is_change = key == CHANGE_KEY
    head, retention = (HEADER, RETENTION) if is_change else (HEADER_B, RETENTION_B)
    if key in BACK_KEYS:
        head = head.replace("\nsummary:", "\n" + BACK_NOTE + "\nsummary:", 1)
        retention = retention.rstrip("\n") + "\n" + BACK_RETENTION + "\n"
    lock = LOCK_CHANGE if is_change else LOCK_B
    return (head + "\ndetailed_description:\n" + action +
            "\n" + retention + FOOTER + lock + TAIL)
