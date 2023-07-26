# QC_gmm_inverse_var_252.py

from AlgorithmImports import *

import pandas as pd
import numpy as np
from math import ceil, floor
import scipy.stats as stats
import sklearn.mixture as mix
from datetime import datetime, timedelta
import time
import decimal as d
import json

# -----------------------------------------------------------------------------
# function for gmm
# -----------------------------------------------------------------------------

def calc_quantile_var(data, alpha=0.05):
    """
    compute var by quantile
    """
    return data.quantile(alpha)


def calc_historical_var(data, alpha=0.05):
    """
    compute historical VAR
    """
    if isinstance(data, pd.DataFrame):
        data = data.squeeze()
    return calc_quantile_var(data, alpha=alpha)


def gmm(data, n_components, max_iter=150, random_state=0, **kwds):
    """
    gaussian mixture model by sklearn
    """
    model = mix.GaussianMixture(
        n_components, max_iter=max_iter, random_state=random_state, **kwds
    )
    model.fit(data)
    return model


def gmm_sample(
    data,
    n_components=2,
    max_iter=150,
    random_state=0,
    n_samples=1000,
    risky=True,
    **kwds
):
    """
    sample from the risky component
    """
    model = gmm(
        data,
        n_components=n_components,
        max_iter=max_iter,
        random_state=random_state,
        **kwds
    )
    X_s, y_s = model.sample(n_samples)
    df = pd.DataFrame(X_s, columns=data.columns).assign(component=y_s)
    if not risky:
        ser = pd.Series(X_s.ravel())
        ser.name = "gmm"
        return ser

    risky = df.groupby("component").mean().mean(1).argmin()
    ser = df.query("component==@risky").set_index("component").squeeze()
    ser.name = "gmm_risky"
    return ser


def calc_gmm_var(
    data,
    n_components=2,
    max_iter=150,
    random_state=0,
    n_samples=1000,
    risky=True,
    **kwds
):
    """
    compute quantile var for gmm risky component
    """
    gmm_samples = gmm_sample(data, n_components, risky=risky)
    return calc_quantile_var(gmm_samples)


# -----------------------------------------------------------------------------
# updating historical timeseries dataframes
# -----------------------------------------------------------------------------


def make_update_df(old, new, lookback):
    """combines and cleans numeric timeseries dataframes
       for updates
    # args
        old, new: pandas dataframes
        lookback: numeric
    # returns
        both: combined dataframe
    """
    # combine datasets
    both = pd.concat([old, new])
    # clean it up and keep only lookback period
    return both.drop_duplicates().sort_index().iloc[-lookback:]


# -----------------------------------------------------------------------------
# order execution functions
# -----------------------------------------------------------------------------


def get_open_order_secs(open_orders):
    """func to return list of symbols
        if open order list is populated
    """
    if open_orders:  # if list is populated
        open_order_secs = [order.Symbol for order in open_orders]
    else:
        open_order_secs = []
    return open_order_secs

# -----------------------------------------------------------------------------
# algorithm class
# -----------------------------------------------------------------------------

class GMMInverseVAR(QCAlgorithm):
    """
    """

    def Initialize(self):
        """Initial algorithm settings"""

        self.INIT_PORTFOLIO_CASH = 1000000

        self.SetStartDate(2008, 5, 1)  # Set Start Date
        self.SetEndDate(2023, 7, 25)  # Set End Date
        
        self.SetCash(self.INIT_PORTFOLIO_CASH)  # Set Strategy Cash

        # -----------------------------------------------------------------------------
        # init brokerage model, important for realistic slippage/commission modeling
        # especially important if using leverage which requires margin account
        # -----------------------------------------------------------------------------

        self.SetBrokerageModel(
            BrokerageName.InteractiveBrokersBrokerage, AccountType.Margin
        )

        # -----------------------------------------------------------------------------
        # init custom universe
        # -----------------------------------------------------------------------------

        self.BASE_SYMBOL = "SPY"

        self.symbols = [
            self.BASE_SYMBOL,
            "QQQ",
            "DIA",
            "TLT",
            "GLD",
            "EFA",
            "EEM",
            "BND",
            "VNQ",
        ]

        for sym in self.symbols:
            self.AddEquity(sym, Resolution.Minute)

        # -----------------------------------------------------------------------------
        # Algo Exchange Settings
        #   Using SPY here as it is synonymous with the Equity market. If trading diff
        #   assets like Futures then one would need to change the symbol used.
        # -----------------------------------------------------------------------------

        self.exchange = self.Securities[self.BASE_SYMBOL].Exchange

        # -----------------------------------------------------------------------------
        # other algo parameter settings
        # -----------------------------------------------------------------------------

        self._init_prices = list()

        self.LOOKBACK = 252  # Historical lookback trading days
        self.LEVERAGE = 1.5
        self.TOLERANCE = 0.025  # +/- target weight
        self.N_COMPONENTS = 2

        self.RANDOM_STATE = 7

        ## set resolution for historical data calls
        self.HISTORY_RESOLUTION = Resolution.Daily

        # -----------------------------------------------------------------------------
        # track RAM and computation time for main func, also leverage and cash
        # -----------------------------------------------------------------------------

        self.splotName = "Strategy Info"
        sPlot = Chart(self.splotName)
        sPlot.AddSeries(Series("RAM", SeriesType.Line, 0))
        sPlot.AddSeries(Series("Time", SeriesType.Line, 1))
        sPlot.AddSeries(Series("Cash", SeriesType.Line, 2))
        sPlot.AddSeries(Series("Leverage", SeriesType.Line, 3))
        self.AddChart(sPlot)

        self.time_to_run_main_algo = 0

        # -----------------------------------------------------------------------------
        # track portfolio weights by symbol
        # -----------------------------------------------------------------------------

        self.splotName3 = "Security Weights Info"
        sPlot3 = Chart(self.splotName3)

        for i, sec in enumerate(self.symbols):
            sPlot3.AddSeries(Series(sec, SeriesType.Line, i))

        self.AddChart(sPlot3)

        # -----------------------------------------------------------------------------
        # scheduled functions
        # -----------------------------------------------------------------------------

        self.Schedule.On(
            self.DateRules.EveryDay(self.BASE_SYMBOL),
            #self.DateRules.WeekStart(self.BASE_SYMBOL),
            #self.DateRules.MonthStart(self.BASE_SYMBOL),
            self.TimeRules.AfterMarketOpen(self.BASE_SYMBOL, 5),
            Action(self.init_prices),
        )

        # make buy list
        self.Schedule.On(
            self.DateRules.EveryDay(self.BASE_SYMBOL),
            #self.DateRules.WeekStart(self.BASE_SYMBOL),
            #self.DateRules.MonthStart(self.BASE_SYMBOL),
            self.TimeRules.AfterMarketOpen(self.BASE_SYMBOL, 10),
            Action(self.rebalance),
        )

        # plot RAM
        self.Schedule.On(
            self.DateRules.EveryDay(self.BASE_SYMBOL),
            self.TimeRules.AfterMarketOpen(self.BASE_SYMBOL, 40),
            Action(self.CHART_RAM),
        )

        # plot weights by asset symbol
        self.Schedule.On(
            self.DateRules.EveryDay(self.BASE_SYMBOL),
            self.TimeRules.BeforeMarketClose(self.BASE_SYMBOL, 70),
            Action(self.CHART_SECURITY_WEIGHTS),
        )

    def init_prices(self):
        """
        Initialize historical prices.
        Cache the price data so we don't have to request the entire df at each
        History call.
        """
        if not self.symbols:
            self.Log("no symbols")
            return

        if self._init_prices:
            return
        self.prices = (
            self.History(self.symbols, self.LOOKBACK, self.HISTORY_RESOLUTION)["close"]
            .unstack(level=0)
            .astype(np.float32)
        )
        self._init_prices = True
        return

    def update_prices(self):
        """
        Update prices efficiently
        NOTES:
        Works by computing the difference between current timestamp and maximum
        price timestamp in total seconds. Then converting the difference based
        on the HISTORY RESOLUTION chosen. Then makes a call to self.History for
        the difference in periods, combines and cleans up the df and updates
        the self.prices variable.
        - USES 'close' PRICES BY DEFAULT
        """

        # get last date of stored prices
        most_recent_date = self.prices.index.max()
        current_date = self.Time

        # how many periods do we need
        diff_in_seconds = (current_date - most_recent_date).total_seconds()

        if self.HISTORY_RESOLUTION == Resolution.Daily:
            diff_to_request = diff_in_seconds // 86400  # seconds in a day
        elif self.HISTORY_RESOLUTION == Resolution.Hour:
            diff_to_request = diff_in_seconds // 3600  # seconds in an hour
        elif self.HISTORY_RESOLUTION == Resolution.Minute:
            diff_to_request = diff_in_seconds // 60  # seconds in a minute

        diff_to_request = int(diff_to_request)  # make sure int dtype

        # if prices up to date return
        if diff_to_request == 0:
            return

        # get new data
        new_prices = self.History(
            self.symbols, diff_to_request, self.HISTORY_RESOLUTION
        )
        if "close" in new_prices.columns:
            new_prices = new_prices["close"].unstack(level=0).astype(np.float32)
        else:
            return
        # combine datasets
        self.prices = make_update_df(self.prices, new_prices, self.LOOKBACK)
        return

    def check_current_weight(self, symbol):
        """
        Check current symbol portfolio weight
        :param symbol: str
        :return current_weight: float
        """
        P = self.Portfolio
        current_weight = float(P[symbol].HoldingsValue) / float(P.TotalPortfolioValue)
        return current_weight

    def rebalance(self):
        """fn: run main algorithm"""
        self.Log(
            "\n"
            + "-" * 77
            + "\n[{}] Begin main algorithm computation...".format(self.UtcTime)
        )

        start_time = time.time()  # timer
        self.update_prices()  # update prices

        returns = np.log(self.prices / self.prices.shift(1)).dropna()

        X = returns.copy()

        # compute var
        var_dict = {}
        for sec in self.symbols:
            var = calc_gmm_var(X[sec].to_frame(), self.N_COMPONENTS, risky=False)
            var_dict[sec] = var

        self.Log("var dict:\n{}".format(var_dict))

        # compute target weights
        var_ser = pd.DataFrame.from_dict(var_dict, orient="index").squeeze()
        invert = 1 / var_ser.abs()
        target_weights = invert / invert.sum()

        self.Log("inverse var weights: {}".format(target_weights))

        for sec in self.symbols:
            # get current weights
            current_weight = self.check_current_weight(sec)
            target_weight = target_weights[sec] * self.LEVERAGE

            # if current weights outside of tolerance send new orders
            tol = self.TOLERANCE * target_weight
            lower_bound = target_weight - tol
            upper_bound = target_weight + tol

            if (current_weight < lower_bound) or (current_weight > upper_bound):
                self.SetHoldings(sec, target_weight)

        ## end timer
        self.time_to_run_main_algo = time.time() - start_time
        self.Plot(self.splotName, "Time", self.time_to_run_main_algo)
        return

    def OnData(self, data):
        """OnData event is the primary entry point for your algorithm.
        Each new data point will be pumped in here."""
        pass

    def CHART_RAM(self):
        # Once a day or something reasonable to prevent spam
        self.Plot(self.splotName, "RAM", OS.ApplicationMemoryUsed / 1024.0)
        P = self.Portfolio
        self.track_account_leverage = (
            P.TotalAbsoluteHoldingsCost / P.TotalPortfolioValue
        )
        self.Plot(self.splotName, "Leverage", float(self.track_account_leverage))
        self.Plot(self.splotName, "Cash", float(self.Portfolio.Cash))
        return

    def CHART_SECURITY_WEIGHTS(self):
        # Once a day or something reasonable to prevent spam
        P = self.Portfolio
        for sec in self.symbols:
            self.Plot(
                self.splotName3,
                sec,
                float(P[sec].HoldingsValue) / float(P.TotalPortfolioValue) * 100,
            )
        return
