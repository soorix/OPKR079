import copy

from cereal import car
from common.realtime import DT_CTRL
from common.numpy_fast import clip
from selfdrive.car import apply_std_steer_torque_limits
from selfdrive.car.hyundai.hyundaican import create_lkas11, create_clu11, create_lfa_mfa, \
                                             create_scc11, create_scc12,  create_scc13, create_scc14, \
                                             create_mdps12, create_spas11, create_spas12, create_ems11
from selfdrive.car.hyundai.values import Buttons, SteerLimitParams, CAR, FEATURES
from opendbc.can.packer import CANPacker
from selfdrive.config import Conversions as CV

from selfdrive.controls.lib.pathplanner import LANE_CHANGE_SPEED_MIN

# speed controller
from selfdrive.car.hyundai.spdcontroller  import SpdController
from selfdrive.car.hyundai.spdctrl  import Spdctrl

from common.params import Params
import common.log as trace1
import common.CTime1000 as tm

VisualAlert = car.CarControl.HUDControl.VisualAlert
min_set_speed = 30 * CV.KPH_TO_MS

# Accel limits
ACCEL_HYST_GAP = 0.02  # don't change accel command for small oscilalitons within this value
ACCEL_MAX = 1.5  # 1.5 m/s2
ACCEL_MIN = -3.0 # 3   m/s2
ACCEL_SCALE = max(ACCEL_MAX, -ACCEL_MIN)
# SPAS steering limits
STEER_ANG_MAX = 360          # SPAS Max Angle
STEER_ANG_MAX_RATE = 1.5    # SPAS Degrees per ms

def accel_hysteresis(accel, accel_steady):

  # for small accel oscillations within ACCEL_HYST_GAP, don't change the accel command
  if accel > accel_steady + ACCEL_HYST_GAP:
    accel_steady = accel - ACCEL_HYST_GAP
  elif accel < accel_steady - ACCEL_HYST_GAP:
    accel_steady = accel + ACCEL_HYST_GAP
  accel = accel_steady

  return accel, accel_steady

def process_hud_alert(enabled, fingerprint, visual_alert, left_lane,
                      right_lane, left_lane_depart, right_lane_depart, button_on):
  sys_warning = (visual_alert == VisualAlert.steerRequired)

  # initialize to no line visible
  sys_state = 1
  if not button_on:
    lane_visible = 0
  if left_lane and right_lane or sys_warning:  #HUD alert only display when LKAS status is active
    if enabled or sys_warning:
      sys_state = 3
    else:
      sys_state = 4
  elif left_lane:
    sys_state = 5
  elif right_lane:
    sys_state = 6

  # initialize to no warnings
  left_lane_warning = 0
  right_lane_warning = 0
  #if left_lane_depart:
  #  left_lane_warning = 1 if fingerprint in [CAR.GENESIS, CAR.GENESIS_G70, CAR.GENESIS_G80,
  #                                           CAR.GENESIS_G90, CAR.GENESIS_G90_L] else 2
  #if right_lane_depart:
  #  right_lane_warning = 1 if fingerprint in [CAR.GENESIS, CAR.GENESIS_G70, CAR.GENESIS_G80,
  #                                            CAR.GENESIS_G90, CAR.GENESIS_G90_L] else 2

  return sys_warning, sys_state, left_lane_warning, right_lane_warning


class CarController():
  def __init__(self, dbc_name, CP, VM):
    self.car_fingerprint = CP.carFingerprint
    self.packer = CANPacker(dbc_name)
    self.accel_steady = 0
    self.apply_steer_last = 0
    self.steer_rate_limited = False
    self.lkas11_cnt = 0
    self.scc12_cnt = 0

    self.resume_cnt = 0
    self.last_lead_distance = 0
    self.resume_wait_timer = 0
    self.lanechange_manual_timer = 0
    self.emergency_manual_timer = 0
    self.driver_steering_torque_above_timer = 0
    
    self.mode_change_timer = 0
    
    self.params = Params()
    self.mode_change_switch = int(self.params.get('CruiseStatemodeSelInit'))
    self.opkr_variablecruise = int(self.params.get('OpkrVariableCruise'))
    self.opkr_autoresume = int(self.params.get('OpkrAutoResume'))

    self.steer_mode = ""
    self.mdps_status = ""
    self.lkas_switch = ""

    self.timer1 = tm.CTime1000("time")
    
    if self.opkr_variablecruise == 1:
      self.SC = Spdctrl()
    else:
      self.SC = None
    
    self.model_speed = 0
    self.model_sum = 0

    self.dRel = 0
    self.yRel = 0
    self.vRel = 0

    self.cruise_gap = 0
    self.cruise_gap_prev = 0
    self.cruise_gap_set_init = 0
    self.cruise_gap_switch_timer = 0

    self.lkas_button_on = True
    self.longcontrol = CP.openpilotLongitudinalControl
    self.scc_live = not CP.radarOffCan

    if CP.spasEnabled:
      self.en_cnt = 0
      self.apply_steer_ang = 0.0
      self.en_spas = 3
      self.mdps11_stat_last = 0
      self.spas_always = False

  def update(self, enabled, CS, frame, CC, actuators, pcm_cancel_cmd, visual_alert,
             left_lane, right_lane, left_lane_depart, right_lane_depart, set_speed, lead_visible, sm):

    # *** compute control surfaces ***

    # gas and brake
    apply_accel = actuators.gas - actuators.brake

    apply_accel, self.accel_steady = accel_hysteresis(apply_accel, self.accel_steady)
    apply_accel = clip(apply_accel * ACCEL_SCALE, ACCEL_MIN, ACCEL_MAX)

    self.dRel, self.yRel, self.vRel = SpdController.get_lead(sm)
    self.model_speed, self.model_sum = self.SC.calc_va(sm, CS.out.vEgo)


    # Steering Torque

    # steerAngleAbs = abs(actuators.steerAngle)
    # limitParams = copy.copy(SteerLimitParams)
    # limitParams.STEER_MAX = int(interp(steerAngleAbs, [5., 10., 20., 30., 50.], [255, SteerLimitParams.STEER_MAX, 500, 600, 700]))
    # limitParams.STEER_DELTA_UP = int(interp(steerAngleAbs, [5., 10., 50.], [2, 3, 4]))
    # limitParams.STEER_DELTA_DOWN = int(interp(steerAngleAbs, [5., 10., 50.], [4, 5, 6]))

    if self.driver_steering_torque_above_timer:
      new_steer = actuators.steer * SteerLimitParams.STEER_MAX * (self.driver_steering_torque_above_timer / 100)
    else:
      new_steer = actuators.steer * SteerLimitParams.STEER_MAX
    apply_steer = apply_std_steer_torque_limits(new_steer, self.apply_steer_last, CS.out.steeringTorque, SteerLimitParams)
    self.steer_rate_limited = new_steer != apply_steer

    CC.applyAccel = apply_accel
    CC.applySteer = apply_steer

    # SPAS limit angle extremes for safety
    if CS.spas_enabled:
      apply_steer_ang_req = clip(actuators.steerAngle, -1*(STEER_ANG_MAX), STEER_ANG_MAX)
      # SPAS limit angle rate for safety
      if abs(self.apply_steer_ang - apply_steer_ang_req) > STEER_ANG_MAX_RATE:
        if apply_steer_ang_req > self.apply_steer_ang:
          self.apply_steer_ang += STEER_ANG_MAX_RATE
        else:
          self.apply_steer_ang -= STEER_ANG_MAX_RATE
      else:
        self.apply_steer_ang = apply_steer_ang_req
    spas_active = CS.spas_enabled and enabled and (self.spas_always or CS.out.vEgo < 7.0) # 25km/h

    # disable if steer angle reach 90 deg, otherwise mdps fault in some models
    # temporarily disable steering when LKAS button off 
    #lkas_active = enabled and abs(CS.out.steeringAngle) < 90. and not spas_active
    lkas_active = enabled and abs(CS.out.steeringAngle) < 90. and not spas_active

    if (( CS.out.leftBlinker and not CS.out.rightBlinker) or ( CS.out.rightBlinker and not CS.out.leftBlinker)) and CS.out.vEgo < LANE_CHANGE_SPEED_MIN:
      self.lanechange_manual_timer = 10
    if CS.out.leftBlinker and CS.out.rightBlinker:
      self.emergency_manual_timer = 10
    if abs(CS.out.steeringTorque) > 200:
      self.driver_steering_torque_above_timer = 100
    if self.lanechange_manual_timer:
      lkas_active = 0
    if self.lanechange_manual_timer > 0:
      self.lanechange_manual_timer -= 1
    if self.emergency_manual_timer > 0:
      self.emergency_manual_timer -= 1
    if self.driver_steering_torque_above_timer > 0:
      self.driver_steering_torque_above_timer -= 1

    if not lkas_active:
      apply_steer = 0

    self.apply_accel_last = apply_accel
    self.apply_steer_last = apply_steer

    sys_warning, sys_state, left_lane_warning, right_lane_warning =\
      process_hud_alert(lkas_active, self.car_fingerprint, visual_alert,
                        left_lane, right_lane, left_lane_depart, right_lane_depart,
                        self.lkas_button_on)

    clu11_speed = CS.clu11["CF_Clu_Vanz"]
    enabled_speed = 38 if CS.is_set_speed_in_mph  else 55
    if clu11_speed > enabled_speed or not lkas_active:
      enabled_speed = clu11_speed

    if not(min_set_speed < set_speed < 255 * CV.KPH_TO_MS):
      set_speed = min_set_speed 
    set_speed *= CV.MS_TO_MPH if CS.is_set_speed_in_mph else CV.MS_TO_KPH

    if frame == 0: # initialize counts from last received count signals
      self.lkas11_cnt = CS.lkas11["CF_Lkas_MsgCount"]
      self.scc12_cnt = CS.scc12["CR_VSM_Alive"] + 1 if not CS.no_radar else 0

      #TODO: fix this
      # self.prev_scc_cnt = CS.scc11["AliveCounterACC"]
      # self.scc_update_frame = frame

    # check if SCC is alive
    # if frame % 7 == 0:
      # if CS.scc11["AliveCounterACC"] == self.prev_scc_cnt:
        # if frame - self.scc_update_frame > 20 and self.scc_live:
          # self.scc_live = False
      # else:
        # self.scc_live = True
        # self.prev_scc_cnt = CS.scc11["AliveCounterACC"]
        # self.scc_update_frame = frame

    self.prev_scc_cnt = CS.scc11["AliveCounterACC"]

    self.lkas11_cnt = (self.lkas11_cnt + 1) % 0x10
    self.scc12_cnt %= 0xF

    can_sends = []
    can_sends.append(create_lkas11(self.packer, frame, self.car_fingerprint, apply_steer, lkas_active,
                                   CS.lkas11, sys_warning, sys_state, enabled, left_lane, right_lane,
                                   left_lane_warning, right_lane_warning, 0))

    if CS.mdps_bus or CS.scc_bus == 1: # send lkas11 bus 1 if mdps or scc is on bus 1
      can_sends.append(create_lkas11(self.packer, frame, self.car_fingerprint, apply_steer, lkas_active,
                                   CS.lkas11, sys_warning, sys_state, enabled, left_lane, right_lane,
                                   left_lane_warning, right_lane_warning, 1))
    if frame % 2 and CS.mdps_bus: # send clu11 to mdps if it is not on bus 0
      can_sends.append(create_clu11(self.packer, frame, CS.mdps_bus, CS.clu11, Buttons.NONE, enabled_speed))


    str_log1 = '곡률={:03.0f}  토크={:03.0f}'.format(abs(self.model_speed), abs(new_steer))
    str_log2 = '프레임률={:03.0f}'.format(self.timer1.sampleTime())
    trace1.printf1( '{}  {}'.format(str_log1, str_log2))

    if CS.out.cruiseState.modeSel == 0 and self.mode_change_switch == 3:
      self.mode_change_timer = 50
      self.mode_change_switch = 0
    elif CS.out.cruiseState.modeSel == 1 and self.mode_change_switch == 0:
      self.mode_change_timer = 50
      self.mode_change_switch = 1
    elif CS.out.cruiseState.modeSel == 2 and self.mode_change_switch == 1:
      self.mode_change_timer = 50
      self.mode_change_switch = 2
    elif CS.out.cruiseState.modeSel == 3 and self.mode_change_switch == 2:
      self.mode_change_timer = 50
      self.mode_change_switch = 3
    if self.mode_change_timer > 0:
      self.mode_change_timer -= 1

    run_speed_ctrl = self.opkr_variablecruise and CS.acc_active and self.SC != None and (CS.out.cruiseState.modeSel == 1 or CS.out.cruiseState.modeSel == 2 or CS.out.cruiseState.modeSel == 3)
    if not run_speed_ctrl:
      if CS.out.cruiseState.modeSel == 0:
        self.steer_mode = "오파모드"
      elif CS.out.cruiseState.modeSel == 1:
        self.steer_mode = "차간+커브"
      elif CS.out.cruiseState.modeSel == 2:
        self.steer_mode = "차간ONLY"
      elif CS.out.cruiseState.modeSel == 3:
        self.steer_mode = "편도1차선"
      if CS.out.steerWarning == 0:
        self.mdps_status = "정상"
      elif CS.out.steerWarning == 1:
        self.mdps_status = "오류"
      if CS.lkas_button_on == 0:
        self.lkas_switch = "OFF"
      elif CS.lkas_button_on == 1:
        self.lkas_switch = "ON"
      else:
        self.lkas_switch = "-"
      self.cruise_gap = CS.cruiseGapSet


      str_log2 = '주행모드={:s}  MDPS상태={:s}  LKAS버튼={:s}  크루즈갭={:1.0f}'.format( self.steer_mode, self.mdps_status, self.lkas_switch, self.cruise_gap )
      trace1.printf2( '{}'.format( str_log2 ) )


    if pcm_cancel_cmd and self.longcontrol:
      can_sends.append(create_clu11(self.packer, frame, CS.scc_bus, CS.clu11, Buttons.CANCEL, clu11_speed))

    if CS.out.cruiseState.standstill:
      if self.last_lead_distance == 0 or not self.opkr_autoresume:
        self.last_lead_distance = CS.lead_distance
        self.resume_cnt = 0
        self.resume_wait_timer = 0

      elif self.resume_wait_timer > 0:
        self.resume_wait_timer -= 1

      elif CS.lead_distance != self.last_lead_distance:
        can_sends.append(create_clu11(self.packer, frame, CS.scc_bus, CS.clu11, Buttons.RES_ACCEL, clu11_speed))
        self.resume_cnt += 1

        if self.resume_cnt > 5:
          self.resume_cnt = 0
          self.resume_wait_timer = int(0.25 / DT_CTRL)

      elif self.cruise_gap_prev == 0: 
        self.cruise_gap_prev = CS.cruiseGapSet
        self.cruise_gap_set_init = 1
      elif CS.cruiseGapSet != 1.0:
        self.cruise_gap_switch_timer += 1
        if self.cruise_gap_switch_timer > 100:
          can_sends.append(create_clu11(self.packer, frame, CS.scc_bus, CS.clu11, Buttons.GAP_DIST, clu11_speed))
          self.cruise_gap_switch_timer = 0

    # reset lead distnce after the car starts moving
    elif self.last_lead_distance != 0:
      self.last_lead_distance = 0
    elif run_speed_ctrl and self.SC != None:
      is_sc_run = self.SC.update(CS, sm, self)
      if is_sc_run:
        can_sends.append(create_clu11(self.packer, self.resume_cnt, CS.scc_bus, CS.clu11, self.SC.btn_type, self.SC.sc_clu_speed))
        self.resume_cnt += 1
      else:
        self.resume_cnt = 0
      if self.dRel > 17 and self.cruise_gap_prev != CS.cruiseGapSet and self.cruise_gap_set_init == 1:
        self.cruise_gap_switch_timer += 1
        if self.cruise_gap_switch_timer > 50:
          can_sends.append(create_clu11(self.packer, frame, CS.scc_bus, CS.clu11, Buttons.GAP_DIST, clu11_speed))
          self.cruise_gap_switch_timer = 0
      elif self.cruise_gap_prev == CS.cruiseGapSet:
        self.cruise_gap_set_init = 0
        self.cruise_gap_prev = 0

    if CS.mdps_bus: # send mdps12 to LKAS to prevent LKAS error
      can_sends.append(create_mdps12(self.packer, frame, CS.mdps12))

    # send scc to car if longcontrol enabled and SCC not on bus 0 or ont live
    if self.longcontrol and (CS.scc_bus or not self.scc_live) and frame % 2 == 0: 
      can_sends.append(create_scc12(self.packer, apply_accel, enabled, self.scc12_cnt, self.scc_live, CS.scc12))
      can_sends.append(create_scc11(self.packer, frame, enabled, set_speed, lead_visible, self.scc_live, CS.scc11))
      if CS.has_scc13 and frame % 20 == 0:
        can_sends.append(create_scc13(self.packer, CS.scc13))
      if CS.has_scc14:
        can_sends.append(create_scc14(self.packer, enabled, CS.scc14))
      self.scc12_cnt += 1

    # 20 Hz LFA MFA message
    if frame % 5 == 0 and self.car_fingerprint in FEATURES["send_lfa_mfa"]:
      can_sends.append(create_lfa_mfa(self.packer, frame, lkas_active))

    if CS.spas_enabled:
      if CS.mdps_bus:
        can_sends.append(create_ems11(self.packer, CS.ems11, spas_active))

      # SPAS11 50hz
      if (frame % 2) == 0:
        if CS.mdps11_stat == 7 and not self.mdps11_stat_last == 7:
          self.en_spas = 7
          self.en_cnt = 0

        if self.en_spas == 7 and self.en_cnt >= 8:
          self.en_spas = 3
          self.en_cnt = 0
  
        if self.en_cnt < 8 and spas_active:
          self.en_spas = 4
        elif self.en_cnt >= 8 and spas_active:
          self.en_spas = 5

        if not spas_active:
          self.apply_steer_ang = CS.mdps11_strang
          self.en_spas = 3
          self.en_cnt = 0

        self.mdps11_stat_last = CS.mdps11_stat
        self.en_cnt += 1
        can_sends.append(create_spas11(self.packer, self.car_fingerprint, (frame // 2), self.en_spas, self.apply_steer_ang, CS.mdps_bus))

      # SPAS12 20Hz
      if (frame % 5) == 0:
        can_sends.append(create_spas12(CS.mdps_bus))

    return can_sends
